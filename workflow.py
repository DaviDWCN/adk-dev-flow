from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from google.adk import Agent, Event, Runner, Workflow
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START
from google.genai import types

from codebuddy_bridge import CodeBuddyLlm, run_codebuddy_worker
from gitlab_client import GitLabClient
from models import ReviewVerdict, SpecContract, StateStore

MAX_REPAIR_ATTEMPTS = int(os.environ.get("ADK_MAX_RETRIES", "3"))


def _run_cmd(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def build_dev_workflow(repo_root: Path, store: StateStore, gl: GitLabClient) -> Workflow:
    def prepare_worktree_node(node_input: Any) -> dict[str, Any]:
        text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
        payload = json.loads(text)
        run_id: str = payload["run_id"]
        spec = SpecContract.model_validate(payload["spec"])

        branch = f"adk/issue-{spec.issue_iid or 'task'}-{run_id[:6]}"
        worktree = repo_root / ".adk" / "worktrees" / run_id
        worktree.parent.mkdir(parents=True, exist_ok=True)

        if not worktree.exists():
            res = _run_cmd(
                ["git", "worktree", "add", "-b", branch, str(worktree), spec.target_branch],
                repo_root,
            )
            if res.returncode != 0:
                res = _run_cmd(
                    ["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"],
                    repo_root,
                )
                if res.returncode != 0:
                    raise RuntimeError(f"创建 Git Worktree 失败: {res.stderr}")

        test_file = worktree / spec.acceptance_test_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(spec.acceptance_test_code, encoding="utf-8")
        _run_cmd(["git", "add", spec.acceptance_test_path], worktree)
        _run_cmd(["git", "commit", "-m", f"test: lock acceptance test for {spec.title}"], worktree)

        store.update_run(
            run_id, status="RUNNING", branch=branch, worktree_path=str(worktree), attempt=0
        )
        return {
            "run_id": run_id,
            "spec": spec.model_dump(),
            "branch": branch,
            "worktree": str(worktree),
            "attempt": 0,
            "prev_error_sig": "",
            "last_error_log": "",
        }

    async def execute_worker_node(node_input: dict[str, Any]) -> dict[str, Any]:
        run_id = node_input["run_id"]
        spec = SpecContract.model_validate(node_input["spec"])
        worktree = Path(node_input["worktree"])
        attempt = int(node_input["attempt"])

        tasks_md = "\n".join(
            f"- [{t.id}] {t.title}: {t.description} (允许文件: {', '.join(t.target_files)})"
            for t in spec.tasks
        )
        if attempt == 0:
            prompt = (
                f"请严格依据以下冻结 Spec 实现功能，严禁修改锁定的验收测试文件 "
                f"`{spec.acceptance_test_path}`。\n\n"
                f"标题: {spec.title}\n摘要: {spec.summary}\n\n任务清单:\n{tasks_md}\n\n"
                f"完成后确保 `ruff check .` 和 `pytest {spec.acceptance_test_path}` 全部通过。"
            )
        else:
            store.update_run(run_id, status="REPAIRING", attempt=attempt)
            prompt = (
                f"第 {attempt}/{MAX_REPAIR_ATTEMPTS} 轮定向修复。上次验收校验失败，"
                f"请仅修复业务代码使校验通过，严禁修改或删除验收测试 `{spec.acceptance_test_path}`。\n\n"
                f"失败日志:\n```\n{node_input['last_error_log'][-3000:]}\n```"
            )

        result = await run_codebuddy_worker(
            worktree=worktree,
            prompt=prompt,
            run_id=run_id,
            allowed_paths=spec.allowed_paths,
            locked_tests=[spec.acceptance_test_path],
        )
        node_input["worker_summary"] = result.get("summary", "")
        return node_input

    def verify_python_node(node_input: dict[str, Any]) -> Event:
        run_id = node_input["run_id"]
        spec = SpecContract.model_validate(node_input["spec"])
        worktree = Path(node_input["worktree"])
        attempt = int(node_input["attempt"])

        ruff_res = _run_cmd(["ruff", "check", "."], worktree)
        pytest_res = _run_cmd(["pytest", "-q", spec.acceptance_test_path], worktree)

        if ruff_res.returncode == 0 and pytest_res.returncode == 0:
            store.update_run(run_id, status="REVIEWING", last_error_log="")
            return Event(output=node_input, route="passed")

        combined_err = (
            f"[ruff exit={ruff_res.returncode}]\n{ruff_res.stdout}\n{ruff_res.stderr}\n"
            f"[pytest exit={pytest_res.returncode}]\n{pytest_res.stdout}\n{pytest_res.stderr}"
        )
        err_sig = hashlib.sha256(combined_err.strip().encode()).hexdigest()[:16]

        next_attempt = attempt + 1
        repeated_same_error = (
            bool(node_input.get("prev_error_sig")) and err_sig == node_input["prev_error_sig"]
        )

        node_input["attempt"] = next_attempt
        node_input["prev_error_sig"] = err_sig
        node_input["last_error_log"] = combined_err
        store.update_run(
            run_id,
            attempt=next_attempt,
            last_error_sig=err_sig,
            last_error_log=combined_err[-4000:],
        )

        if next_attempt >= MAX_REPAIR_ATTEMPTS or repeated_same_error:
            node_input["escalation_reason"] = (
                "连续两轮出现完全相同的报错签名，触发防死循环熔断"
                if repeated_same_error
                else "达到最大自动修复轮次上限"
            )
            return Event(output=node_input, route="escalated")

        return Event(output=node_input, route="retry")

    def prepare_diff_node(node_input: dict[str, Any]) -> dict[str, Any]:
        worktree = Path(node_input["worktree"])
        spec = SpecContract.model_validate(node_input["spec"])
        _run_cmd(["git", "add", "-A"], worktree)
        _run_cmd(["git", "commit", "-m", f"feat: {spec.title}"], worktree)
        diff = _run_cmd(["git", "diff", f"{spec.target_branch}...HEAD"], worktree).stdout
        node_input["git_diff"] = diff[:12000]
        return node_input

    critic_model: Any = os.environ.get("ADK_CRITIC_MODEL") or CodeBuddyLlm()
    critic_review_agent = Agent(
        name="critic_review_agent",
        model=critic_model,
        output_schema=ReviewVerdict,
        instruction=(
            "你是独立的只读代码审查专家（Critic）。你收到的是 JSON 上下文，包含 spec 和 git_diff。"
            "严格检查：1) 是否违背 Spec 约束；2) 是否有明显边界空值、异常处理或安全漏洞；"
            "3) 是否存在硬编码密钥或绕过测试的作弊行为。只输出符合 ReviewVerdict 的 JSON。"
        ),
    )

    def publish_mr_node(node_input: Any) -> dict[str, Any]:
        verdict = (
            ReviewVerdict.model_validate(node_input)
            if isinstance(node_input, dict)
            else ReviewVerdict(approved=True, summary=str(node_input))
        )
        active = json.loads(store.active_marker.read_text(encoding="utf-8"))
        run_id = active["run_id"]
        run = store.get_run(run_id)
        assert run is not None
        spec = SpecContract.model_validate_json(run["spec_json"])
        worktree = Path(run["worktree_path"])
        branch = run["branch"]

        mr_url = ""
        mr_iid = None
        if gl.enabled:
            push = _run_cmd(["git", "push", "-u", "origin", branch], worktree)
            if push.returncode != 0:
                store.update_run(
                    run_id, status="ESCALATED", last_error_log=f"git push 失败: {push.stderr}"
                )
                return {"run_id": run_id, "status": "ESCALATED", "error": push.stderr}
            desc = (
                f"## Spec 摘要\n{spec.summary}\n\n"
                f"## 独立 Critic 审查结论\n- 通过: `{verdict.approved}`\n- 摘要: {verdict.summary}\n"
            )
            if verdict.blocking_issues:
                issues_md = "\n".join(f"- {i}" for i in verdict.blocking_issues)
                desc += f"\n### 需人工关注项\n{issues_md}"
            mr = gl.create_or_update_mr(
                source_branch=branch,
                target_branch=spec.target_branch,
                title=spec.title,
                description=desc,
                draft=not verdict.approved,
            )
            mr_iid, mr_url = mr["iid"], mr["web_url"]

        store.update_run(
            run_id,
            status="MR_READY",
            mr_iid=mr_iid,
            mr_url=mr_url,
            review_json=verdict.model_dump_json(),
        )
        return {
            "run_id": run_id,
            "status": "MR_READY",
            "mr_url": mr_url,
            "review": verdict.model_dump(),
        }

    def escalate_node(node_input: dict[str, Any]) -> dict[str, Any]:
        run_id = node_input["run_id"]
        reason = node_input.get("escalation_reason", "验证失败")
        store.update_run(run_id, status="ESCALATED")
        return {
            "run_id": run_id,
            "status": "ESCALATED",
            "reason": reason,
            "worktree": node_input["worktree"],
            "last_error_log": node_input.get("last_error_log", "")[-2000:],
        }

    return Workflow(
        name="deterministic_python_dev_workflow",
        edges=[
            (START, prepare_worktree_node, execute_worker_node, verify_python_node),
            (
                verify_python_node,
                {
                    "retry": execute_worker_node,
                    "passed": prepare_diff_node,
                    "escalated": escalate_node,
                },
            ),
            (prepare_diff_node, critic_review_agent, publish_mr_node),
        ],
    )


async def execute_workflow_run(
    repo_root: Path, store: StateStore, gl: GitLabClient, run_id: str
) -> None:
    run = store.get_run(run_id)
    if not run or not run.get("spec_json"):
        raise ValueError(f"run_id={run_id} 未找到或尚未锁定 Spec")

    workflow = build_dev_workflow(repo_root, store, gl)
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="adk_dev_flow", user_id="developer", session_id=run_id
    )
    runner = Runner(agent=workflow, app_name="adk_dev_flow", session_service=session_service)

    payload = json.dumps(
        {"run_id": run_id, "spec": json.loads(run["spec_json"])}, ensure_ascii=False
    )
    msg = types.Content(role="user", parts=[types.Part(text=payload)])
    try:
        async for _ in runner.run_async(user_id="developer", session_id=run_id, new_message=msg):
            pass
    except Exception as exc:
        store.update_run(run_id, status="ESCALATED", last_error_log=f"Workflow 异常: {exc!r}")
