from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from google.adk import Agent, Event, Runner, Workflow
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START
from google.genai import types

from adk_dev_flow.codebuddy_bridge import CodeBuddyLlm, run_codebuddy_worker
from adk_dev_flow.forge_client import BaseGitForge, GitLabClient, get_git_forge
from adk_dev_flow.models import ReviewVerdict, SpecContract
from adk_dev_flow.providers import get_worker_provider
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import CommandResult, run_subprocess_safe
from adk_dev_flow.worktree_utils import (
    audit_worktree_changes,
    compute_error_signature,
    compute_file_sha256,
    parse_review_verdict_dict,
    resolve_tool_command,
    sanitize_worktree_state,
    sync_worktree_dependencies,
)

MAX_REPAIR_ATTEMPTS = int(os.environ.get("ADK_MAX_RETRIES", "3"))
MAX_DIFF_CHARS = int(os.environ.get("ADK_MAX_DIFF_CHARS", "12000"))
GIT_TIMEOUT_SEC = float(os.environ.get("ADK_GIT_TIMEOUT", "60"))
VERIFY_TIMEOUT_SEC = float(os.environ.get("ADK_VERIFY_TIMEOUT", "180"))


async def _git(cmd: list[str], cwd: Path, timeout_sec: float = GIT_TIMEOUT_SEC) -> CommandResult:
    return await run_subprocess_safe(["git", *cmd], cwd=cwd, timeout_sec=timeout_sec)


def _format_mr_description(spec: SpecContract, verdict: ReviewVerdict) -> str:
    parts = [
        f"## Spec 摘要\n{spec.summary}",
        (
            "## 独立 Critic 审查结论\n"
            f"- 通过: `{verdict.approved}`\n"
            f"- 摘要: {verdict.summary}"
        ),
    ]
    if verdict.blocking_issues:
        issues_md = "\n".join(f"- {issue}" for issue in verdict.blocking_issues)
        parts.append(f"### 需人工关注项\n{issues_md}")
    if spec.issue_iid is not None:
        parts.append(f"Closes #{spec.issue_iid}")
    return "\n\n".join(parts)


def _format_critic_note(run_id: str, verdict: ReviewVerdict, attempt: int) -> str:
    status_label = "APPROVED" if verdict.approved else "CHANGES_REQUESTED (Draft)"
    lines = [
        f"### ADK Critic 独立审查报告 (`{run_id}`)",
        f"- 结论: **{status_label}**",
        f"- 修复轮次: `{attempt}`",
        f"- 摘要: {verdict.summary}",
    ]
    if verdict.blocking_issues:
        lines.append("\n#### 阻塞项")
        lines.extend(f"- {item}" for item in verdict.blocking_issues)
    return "\n".join(lines)


def _load_okf_rules(worktree: Path, repo_root: Path) -> str:
    rules_text: list[str] = []
    seen: set[str] = set()
    for base in (worktree, repo_root):
        rules_dir = base / ".adk" / "knowledge" / "rules"
        if rules_dir.is_dir():
            for rule_file in sorted(rules_dir.glob("*.md")):
                if rule_file.name not in seen:
                    seen.add(rule_file.name)
                    try:
                        content = rule_file.read_text(encoding="utf-8").strip()
                        if content:
                            rules_text.append(f"### {rule_file.stem}\n{content}")
                    except Exception:
                        pass
    if not rules_text:
        return ""
    return "\n\n项目工程规约 (OKF Rules):\n" + "\n\n".join(rules_text)


def build_dev_workflow(
    repo_root: Path,
    store: StateStore,
    gl: BaseGitForge,
    run_id: str,
    human_hint: str | None = None,
) -> Workflow:
    """Build an async ADK 2.x Graph Workflow bound to a specific run_id via closure."""

    async def prepare_worktree_node(node_input: Any) -> dict[str, Any]:
        text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
        payload = json.loads(text)
        spec = SpecContract.model_validate(payload["spec"])

        existing_run = store.get_run(run_id)
        assert existing_run is not None, f"run_id={run_id} 不存在"

        branch = existing_run.get("branch") or f"adk/issue-{spec.issue_iid or 'task'}-{run_id[:6]}"
        worktree = (
            Path(existing_run["worktree_path"])
            if existing_run.get("worktree_path")
            else repo_root / ".adk" / "worktrees" / run_id
        )
        base_sha = existing_run.get("base_sha") or ""
        locked_test_sha256 = existing_run.get("locked_test_sha256") or ""
        locked_test_commit_sha = existing_run.get("locked_test_commit_sha") or ""

        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            res = await _git(
                ["worktree", "add", "-b", branch, str(worktree), spec.target_branch],
                repo_root,
            )
            if res.returncode != 0:
                res = await _git(
                    ["worktree", "add", "-b", branch, str(worktree), "HEAD"],
                    repo_root,
                )
                if res.returncode != 0:
                    raise RuntimeError(f"创建 Git Worktree 失败: {res.stderr.strip()}")

            # Record the exact base commit SHA BEFORE adding the locked acceptance test.
            base_res = await _git(["rev-parse", "HEAD"], worktree)
            if base_res.returncode != 0:
                raise RuntimeError(f"读取基准 commit SHA 失败: {base_res.stderr.strip()}")
            base_sha = base_res.stdout.strip()

            test_file = worktree / spec.acceptance_test_path
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(spec.acceptance_test_code, encoding="utf-8")
            locked_test_sha256 = compute_file_sha256(test_file)

            add_res = await _git(["add", "--", spec.acceptance_test_path], worktree)
            if add_res.returncode != 0:
                raise RuntimeError(f"暂存验收测试失败: {add_res.stderr.strip()}")

            commit_res = await _git(
                ["commit", "-m", f"test: lock acceptance test for {spec.title}"],
                worktree,
            )
            if commit_res.returncode != 0:
                raise RuntimeError(f"提交锁定验收测试失败: {commit_res.stderr.strip()}")

            lock_sha_res = await _git(["rev-parse", "HEAD"], worktree)
            if lock_sha_res.returncode != 0:
                raise RuntimeError(f"读取锁定测试 commit SHA 失败: {lock_sha_res.stderr.strip()}")
            locked_test_commit_sha = lock_sha_res.stdout.strip()

            if spec.extra_dependencies:
                dep_ok, dep_msg = await sync_worktree_dependencies(
                    worktree, repo_root, spec.extra_dependencies
                )
                if not dep_ok:
                    raise RuntimeError(f"安装增量依赖失败: {dep_msg}")

            # Pre-flight TDD Red verification on the clean baseline worktree
            if spec.require_tdd_red and os.environ.get("ADK_SKIP_TDD_RED", "0") != "1":
                red_cmd = resolve_tool_command(
                    "pytest",
                    ["-q", spec.acceptance_test_path],
                    worktree=worktree,
                    repo_root=repo_root,
                    env_override=os.environ.get("ADK_TEST_CMD"),
                    placeholder_vars={"test_path": spec.acceptance_test_path},
                )
                if red_cmd is not None:
                    red_res = await run_subprocess_safe(
                        red_cmd, cwd=worktree, timeout_sec=VERIFY_TIMEOUT_SEC
                    )
                    if red_res.returncode == 0:
                        raise RuntimeError(
                            f"TDD 预检失败：锁定的验收测试 `{spec.acceptance_test_path}` 在未编写业务代码前直接通过 (exit=0)！\n"
                            f"这表明该测试属于无断言空测试（Mock Passes），或该特性在基线代码中已存在。\n"
                            f"请为验收测试增加针对新功能的有效断言，或在 Spec 中设置 require_tdd_red=False。"
                        )
        else:
            await sanitize_worktree_state(worktree)
            # Resume path: ensure metadata is intact.
            if not base_sha:
                base_res = await _git(["rev-parse", "HEAD"], worktree)
                base_sha = base_res.stdout.strip() if base_res.returncode == 0 else "HEAD"
            if not locked_test_sha256:
                test_file = worktree / spec.acceptance_test_path
                if test_file.is_file():
                    locked_test_sha256 = compute_file_sha256(test_file)

        store.update_run(
            run_id,
            status="RUNNING",
            branch=branch,
            worktree_path=str(worktree),
            base_sha=base_sha,
            locked_test_sha256=locked_test_sha256,
            locked_test_commit_sha=locked_test_commit_sha,
            attempt=0,
            last_error_sig="",
            last_error_log=f"[Architect] 📐 规约与测试用例已锁定，Worktree: {worktree.name}",
        )
        return {
            "run_id": run_id,
            "spec": spec.model_dump(),
            "branch": branch,
            "worktree": str(worktree),
            "base_sha": base_sha,
            "locked_test_sha256": locked_test_sha256,
            "locked_test_commit_sha": locked_test_commit_sha,
            "attempt": 0,
            "prev_error_sig": "",
            "last_error_log": existing_run.get("last_error_log") or "" if human_hint else "",
            "human_hint": human_hint or "",
        }

    async def execute_worker_node(node_input: dict[str, Any]) -> dict[str, Any]:
        spec = SpecContract.model_validate(node_input["spec"])
        worktree = Path(node_input["worktree"])
        attempt = int(node_input["attempt"])
        hint = str(node_input.get("human_hint") or "").strip()

        tasks_md = "\n".join(
            f"- [{t.id}] {t.title}: {t.description} (允许文件: {', '.join(t.target_files)})"
            for t in spec.tasks
        )

        if attempt == 0 and hint:
            store.update_run(
                run_id,
                status="REPAIRING",
                attempt=0,
                last_error_log=f"[Worker] 🔨 结合人工指令进入定向修复: {hint[:60]}",
            )
            prev_err = str(node_input.get("last_error_log") or "")[-2000:]
            prompt = (
                f"用户已介入补充修复指令，请严格在白名单 `{spec.allowed_paths}` 内修复代码，"
                f"严禁修改锁定的验收测试 `{spec.acceptance_test_path}`。\n\n"
                f"用户补充指令:\n{hint}\n\n"
                f"Spec 标题: {spec.title}\n任务清单:\n{tasks_md}\n\n"
                f"历史失败日志参考:\n```\n{prev_err}\n```"
            )
            # Consume human_hint once so subsequent automatic retries focus on fresh errors.
            node_input["human_hint"] = ""
        elif attempt == 0:
            store.update_run(
                run_id,
                status="RUNNING",
                attempt=0,
                last_error_log=f"[Worker] 🔨 工兵在隔离沙箱中施工: {spec.title}",
            )
            prompt = (
                f"请严格依据以下冻结 Spec 实现功能，严禁修改锁定的验收测试文件 "
                f"`{spec.acceptance_test_path}`，且仅允许修改白名单路径 {spec.allowed_paths}。\n\n"
                f"标题: {spec.title}\n摘要: {spec.summary}\n\n任务清单:\n{tasks_md}\n\n"
                f"完成后请确保代码检查与验收测试 `{spec.acceptance_test_path}` 全部通过。"
            )
        else:
            store.update_run(
                run_id,
                status="REPAIRING",
                attempt=attempt,
                last_error_log=f"[Worker] 🔨 正在执行第 {attempt}/{MAX_REPAIR_ATTEMPTS} 轮修复",
            )
            prompt = (
                f"第 {attempt}/{MAX_REPAIR_ATTEMPTS} 轮定向修复。上次验收校验失败，"
                f"请仅修复白名单 `{spec.allowed_paths}` 内的业务代码使校验通过，"
                f"严禁修改或删除验收测试 `{spec.acceptance_test_path}`。\n\n"
                f"失败日志:\n```\n{str(node_input.get('last_error_log', ''))[-3000:]}\n```"
            )

        okf_rules = _load_okf_rules(worktree, repo_root)
        if okf_rules:
            prompt += okf_rules

        provider = get_worker_provider()
        result = await provider.run_worker(
            worktree=worktree,
            prompt=prompt,
            run_id=run_id,
            allowed_paths=spec.allowed_paths,
            locked_tests=[spec.acceptance_test_path],
        )
        node_input["worker_summary"] = result.get("summary", "")
        return node_input

    async def verify_python_node(node_input: dict[str, Any]) -> Event:
        spec = SpecContract.model_validate(node_input["spec"])
        worktree = Path(node_input["worktree"])
        attempt = int(node_input["attempt"])

        store.update_run(
            run_id,
            status="VERIFYING",
            attempt=attempt,
            last_error_log=f"[Tester] 🧪 正在对工作区执行审计与 Pytest 验收门禁 (轮次: {attempt})",
        )

        # Step 1: Unbypassable physical Git & SHA-256 audit BEFORE running any tests.
        audit = await audit_worktree_changes(
            worktree,
            allowed_paths=spec.allowed_paths,
            acceptance_test_path=spec.acceptance_test_path,
            locked_test_sha256=str(node_input.get("locked_test_sha256") or ""),
            locked_test_commit_sha=str(node_input.get("locked_test_commit_sha") or ""),
        )

        failure_sections: list[str] = []
        if not audit.ok:
            failure_sections.append(
                "[worktree_audit FAILED]\n" + "\n".join(f"- {v}" for v in audit.violations)
            )
        else:
            if spec.extra_dependencies:
                await sync_worktree_dependencies(worktree, repo_root, spec.extra_dependencies)

            # Step 2: Resolve commands inside target repository venv (or env overrides).
            lint_cmd = resolve_tool_command(
                "ruff",
                ["check", "."],
                worktree=worktree,
                repo_root=repo_root,
                env_override=os.environ.get("ADK_LINT_CMD"),
            )
            if lint_cmd is not None:
                lint_res = await run_subprocess_safe(
                    lint_cmd, cwd=worktree, timeout_sec=VERIFY_TIMEOUT_SEC
                )
                if lint_res.returncode != 0:
                    failure_sections.append(
                        f"[lint cmd={' '.join(lint_cmd)} exit={lint_res.returncode}]\n"
                        f"{lint_res.stdout}\n{lint_res.stderr}"
                    )

            test_cmd = resolve_tool_command(
                "pytest",
                ["-q", spec.acceptance_test_path],
                worktree=worktree,
                repo_root=repo_root,
                env_override=os.environ.get("ADK_TEST_CMD"),
                placeholder_vars={"test_path": spec.acceptance_test_path},
            )
            if test_cmd is not None:
                test_res = await run_subprocess_safe(
                    test_cmd, cwd=worktree, timeout_sec=VERIFY_TIMEOUT_SEC
                )
                if test_res.returncode != 0:
                    failure_sections.append(
                        f"[pytest cmd={' '.join(test_cmd)} exit={test_res.returncode}]\n"
                        f"{test_res.stdout}\n{test_res.stderr}"
                    )

            # Optional full regression / coverage gate matching CI.
            regression_override = os.environ.get("ADK_REGRESSION_CMD")
            if not failure_sections and regression_override:
                reg_cmd = resolve_tool_command(
                    "pytest",
                    ["-q"],
                    worktree=worktree,
                    repo_root=repo_root,
                    env_override=regression_override,
                    placeholder_vars={"test_path": spec.acceptance_test_path},
                )
                if reg_cmd is not None:
                    reg_res = await run_subprocess_safe(
                        reg_cmd, cwd=worktree, timeout_sec=VERIFY_TIMEOUT_SEC
                    )
                    if reg_res.returncode != 0:
                        failure_sections.append(
                            f"[regression cmd={' '.join(reg_cmd)} exit={reg_res.returncode}]\n"
                            f"{reg_res.stdout}\n{reg_res.stderr}"
                        )

        if not failure_sections:
            store.update_run(run_id, status="REVIEWING", last_error_log="")
            return Event(output=node_input, route="passed")

        combined_err = "\n\n".join(failure_sections)
        err_sig = compute_error_signature(combined_err, worktree=worktree)

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

        # Fixed off-by-one: MAX_REPAIR_ATTEMPTS=3 allows initial(0) + repair rounds 1, 2, 3.
        if next_attempt > MAX_REPAIR_ATTEMPTS or repeated_same_error:
            node_input["escalation_reason"] = (
                "连续两轮出现相同的报错指纹，触发防死循环熔断"
                if repeated_same_error
                else f"已达最大自动修复轮次上限 ({MAX_REPAIR_ATTEMPTS})"
            )
            return Event(output=node_input, route="escalated")

        return Event(output=node_input, route="retry")

    async def prepare_diff_node(node_input: dict[str, Any]) -> str:
        worktree = Path(node_input["worktree"])
        spec = SpecContract.model_validate(node_input["spec"])
        base_sha = str(node_input.get("base_sha") or "HEAD~1")

        store.update_run(
            run_id,
            status="REVIEWING",
            attempt=int(node_input.get("attempt") or 0),
            last_error_log="[Critic] 🧐 独立盲审中：正在对 Diff 变更进行架构与安全审查",
        )

        status_res = await _git(["status", "--porcelain"], worktree)
        if status_res.returncode != 0:
            raise RuntimeError(f"git status 失败: {status_res.stderr.strip()}")

        if status_res.stdout.strip():
            add_res = await _git(["add", "-A"], worktree)
            if add_res.returncode != 0:
                raise RuntimeError(f"git add 失败: {add_res.stderr.strip()}")
            commit_res = await _git(["commit", "-m", f"feat: {spec.title}"], worktree)
            if commit_res.returncode != 0:
                raise RuntimeError(f"git commit 失败: {commit_res.stderr.strip()}")

        # 1. 提取完整的变更文件统计作为骨架
        stat_res = await _git(["diff", "--stat", f"{base_sha}..HEAD"], worktree)
        diff_stat = stat_res.stdout.strip() if stat_res.returncode == 0 else ""

        # 2. 提取变更的文件名清单，按文件边界组装 Diff
        names_res = await _git(["diff", "--name-only", f"{base_sha}..HEAD"], worktree)
        changed_files = [line.strip() for line in names_res.stdout.splitlines() if line.strip()]

        assembled_diff_blocks: list[str] = []
        current_len = 0
        truncated_files: list[str] = []

        for f in changed_files:
            file_diff_res = await _git(["diff", f"{base_sha}..HEAD", "--", f], worktree)
            file_diff = file_diff_res.stdout
            if current_len + len(file_diff) <= MAX_DIFF_CHARS:
                assembled_diff_blocks.append(file_diff)
                current_len += len(file_diff)
            else:
                truncated_files.append(f)

        diff_truncated = len(truncated_files) > 0
        diff_text = "\n".join(assembled_diff_blocks)
        if diff_truncated:
            diff_text += (
                f"\n\n... [DIFF TRUNCATED BY FILE BOUNDARY]\n"
                f"以下文件因超出长度预算未展示完整 Diff: {', '.join(truncated_files)}\n"
                f"请重点参考 diff_stat 评估影响面。"
            )

        # Return an explicit JSON string so ADK 2.x reliably delivers text content to Critic Agent.
        critic_payload = {
            "spec": {
                "title": spec.title,
                "summary": spec.summary,
                "allowed_paths": spec.allowed_paths,
                "acceptance_test_path": spec.acceptance_test_path,
                "tasks": [t.model_dump() for t in spec.tasks],
            },
            "diff_stat": diff_stat,
            "worker_summary": node_input.get("worker_summary", ""),
            "diff_truncated": diff_truncated,
            "git_diff": diff_text,
        }
        return json.dumps(critic_payload, ensure_ascii=False, indent=2)

    critic_model: Any = os.environ.get("ADK_CRITIC_MODEL") or CodeBuddyLlm()
    critic_review_agent = Agent(
        name="critic_review_agent",
        model=critic_model,
        output_schema=ReviewVerdict,
        instruction=(
            "你是独立的只读代码审查专家（Critic）。你收到的是 JSON 上下文，包含 spec、worker_summary 和 git_diff。"
            "严格检查：1) 是否违背 Spec 约束或越界修改；2) 是否有明显边界空值、异常处理或安全漏洞；"
            "3) 是否存在硬编码密钥或绕过测试的作弊行为；4) 若 diff_truncated 为 true 且无法确认完整性，请审慎评估并在 blocking_issues 说明。"
            "只输出符合 ReviewVerdict 结构的 JSON。"
        ),
    )

    async def publish_mr_node(node_input: Any) -> dict[str, Any]:
        # Strict Fail-Closed parsing: any invalid/unrecognized output sets approved=False.
        verdict = ReviewVerdict.model_validate(parse_review_verdict_dict(node_input))

        run = store.get_run(run_id)
        if run is None:
            raise RuntimeError(f"run_id={run_id} 在发布阶段未找到")
        spec = SpecContract.model_validate_json(run["spec_json"])
        worktree = Path(run["worktree_path"])
        branch = run["branch"]
        attempt = int(run.get("attempt") or 0)

        mr_url = ""
        mr_iid = None

        # Save and commit OKF review audit document into worktree
        audit_file_rel = f".adk/audit/review_{run_id}.md"
        try:
            audit_file = worktree / ".adk" / "audit" / f"review_{run_id}.md"
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            provenance = {
                "base_sha": run.get("base_sha") or "",
                "locked_test_commit_sha": run.get("locked_test_commit_sha") or "",
                "attempt": attempt,
                "generator": "adk-dev-flow:critic",
            }
            audit_content = verdict.to_okf_markdown(
                run_id=run_id,
                provenance=provenance,
                raw_report=_format_critic_note(run_id, verdict, attempt),
            )
            audit_file.write_text(audit_content, encoding="utf-8")
            add_res = await _git(["add", "-f", "--", audit_file_rel], worktree)
            if add_res.returncode == 0:
                await _git(
                    ["commit", "-m", f"docs(audit): append OKF review verdict for {run_id}"],
                    worktree,
                )
        except Exception:
            # Audit file committing failure should not fail the overall pipeline
            pass

        if gl.enabled:
            push_res = await _git(["push", "-u", "origin", branch], worktree)
            if push_res.returncode != 0:
                err_msg = f"git push 失败: {push_res.stderr.strip()}"
                store.update_run(run_id, status="ESCALATED", last_error_log=err_msg)
                return {"run_id": run_id, "status": "ESCALATED", "error": err_msg}

            desc = _format_mr_description(spec, verdict)
            mr = await asyncio.to_thread(
                gl.create_or_update_mr,
                source_branch=branch,
                target_branch=spec.target_branch,
                title=spec.title,
                description=desc,
                draft=not verdict.approved,
            )
            mr_iid, mr_url = mr["iid"], mr["web_url"]

            try:
                await asyncio.to_thread(
                    gl.post_mr_note,
                    mr_iid,
                    _format_critic_note(run_id, verdict, attempt),
                )
            except Exception:
                # MR note failure should not invalidate an already-published MR.
                pass

        store.update_run(
            run_id,
            status="MR_READY",
            mr_iid=mr_iid,
            mr_url=mr_url,
            review_json=verdict.model_dump_json(),
            last_error_log=f"[MR] 🚀 验收与审查全部通过！已交付 {gl.platform_name.upper()} PR/MR: {mr_url or branch}",
        )
        return {
            "run_id": run_id,
            "status": "MR_READY",
            "mr_iid": mr_iid,
            "mr_url": mr_url,
            "review": verdict.model_dump(),
        }

    async def escalate_node(node_input: dict[str, Any]) -> dict[str, Any]:
        reason = node_input.get("escalation_reason", "验证失败")
        err_log = str(node_input.get("last_error_log", ""))[-1000:]
        store.update_run(
            run_id,
            status="ESCALATED",
            last_error_log=f"[Alert] 🚨 流水线熔断: {reason}\n{err_log}",
        )
        return {
            "run_id": run_id,
            "status": "ESCALATED",
            "reason": reason,
            "worktree": node_input["worktree"],
            "last_error_log": str(node_input.get("last_error_log", ""))[-2000:],
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
    repo_root: Path,
    store: StateStore,
    gl: BaseGitForge,
    run_id: str,
    human_hint: str | None = None,
) -> None:
    run = store.get_run(run_id)
    if not run or not run.get("spec_json"):
        raise ValueError(f"run_id={run_id} 未找到或尚未锁定 Spec")

    workflow = build_dev_workflow(repo_root, store, gl, run_id=run_id, human_hint=human_hint)
    session_service = InMemorySessionService()
    session_id = f"{run_id}-{int(run.get('attempt') or 0)}-{bool(human_hint)}"
    await session_service.create_session(
        app_name="adk_dev_flow", user_id="developer", session_id=session_id
    )
    runner = Runner(agent=workflow, app_name="adk_dev_flow", session_service=session_service)

    payload = json.dumps(
        {"run_id": run_id, "spec": json.loads(run["spec_json"])}, ensure_ascii=False
    )
    msg = types.Content(role="user", parts=[types.Part(text=payload)])
    try:
        async for _ in runner.run_async(
            user_id="developer", session_id=session_id, new_message=msg
        ):
            pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        store.update_run(run_id, status="ESCALATED", last_error_log=f"Workflow 异常: {exc!r}")
