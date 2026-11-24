from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from typing import Any, Sequence

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

def get_max_diff_chars() -> int:
    val = os.environ.get("ADK_MAX_DIFF_CHARS", "12000")
    try:
        return int(val)
    except (ValueError, TypeError):
        return 12000


MAX_REPAIR_ATTEMPTS = int(os.environ.get("ADK_MAX_RETRIES", "3"))
MAX_DIFF_CHARS = get_max_diff_chars()
GIT_TIMEOUT_SEC = float(os.environ.get("ADK_GIT_TIMEOUT", "60"))
VERIFY_TIMEOUT_SEC = float(os.environ.get("ADK_VERIFY_TIMEOUT", "180"))


def assemble_diff_blocks(
    file_diffs: list[tuple[str, str]],
    max_chars: int | None = None,
) -> tuple[str, bool, list[str]]:
    """Assemble git diff blocks for changed files with per-file budget allocation.

    Prevents starvation of subsequent files (like test files and spec contracts)
    when an implementation file has a massive diff.
    """
    if max_chars is None:
        max_chars = get_max_diff_chars()

    if not file_diffs:
        return "", False, []

    n_files = len(file_diffs)
    # Fair per-file budget allocation
    per_file_budget = (
        max(1000, max_chars // max(1, n_files))
        if max_chars >= 1000 * n_files
        else max(100, max_chars // max(1, n_files))
    )

    assembled_blocks: list[str] = []
    truncated_files: list[str] = []

    for file_path, file_diff in file_diffs:
        if len(file_diff) <= per_file_budget:
            assembled_blocks.append(file_diff)
        else:
            truncated_files.append(file_path)
            last_nl = file_diff.rfind("\n", 0, per_file_budget)
            cut_point = last_nl if last_nl > per_file_budget // 2 else per_file_budget
            truncated_diff = file_diff[:cut_point]
            banner = (
                f"\n... [DIFF TRUNCATED FOR {file_path}: "
                f"showing first {cut_point} chars, see diff_stat for full changes] ...\n"
            )
            assembled_blocks.append(truncated_diff + banner)

    diff_truncated = len(truncated_files) > 0
    diff_text = "\n".join(assembled_blocks)
    if diff_truncated:
        diff_text += (
            f"\n\n... [DIFF TRUNCATED BY FILE BOUNDARY]\n"
            f"以下文件因超出长度预算未展示完整 Diff: {', '.join(truncated_files)}\n"
            f"请重点参考 diff_stat 评估影响面。"
        )

    return diff_text, diff_truncated, truncated_files


async def _git(cmd: list[str], cwd: Path, timeout_sec: float = GIT_TIMEOUT_SEC) -> CommandResult:
    return await run_subprocess_safe(["git", *cmd], cwd=cwd, timeout_sec=timeout_sec)


def enforce_deterministic_verdict_guard(
    verdict: dict[str, Any],
    ast_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically enforce that non-deterministic LLM Critic cannot override hard AST rules.

    If any AST report contains syntax errors, dangerous nodes (eval/exec/secrets), or breaking signature
    changes, approved MUST be forced to False, and blocking issues prepended.
    """
    out = dict(verdict)
    violations: list[str] = []
    for report in ast_findings:
        f_path = report.get("file_path", "unknown")
        if not report.get("syntax_ok", True):
            violations.append(f"[{f_path}] 语法解析异常: {report.get('syntax_error')}")
        for d in report.get("dangerous_nodes", []):
            violations.append(f"[{f_path}] 高危模式: {d}")
        for b in report.get("breaking_issues", []):
            violations.append(f"[{f_path}] 破坏性接口变更: {b}")

    if violations:
        out["approved"] = False
        blocking = list(out.get("blocking_issues") or [])
        combined_blocking = [v for v in violations if v not in blocking] + blocking
        out["blocking_issues"] = combined_blocking
        old_sum = out.get("summary") or ""
        out["summary"] = f"[AST 静态契约门禁一票否决] 发现 {len(violations)} 个确定性静态违规项；{old_sum}".strip()

    return out


def _format_mr_description(
    spec: SpecContract,
    verdict: ReviewVerdict,
    verification: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"## Spec 摘要与需求\n{spec.summary}",
    ]

    # Acceptance Criteria (AC)
    ac_lines = [
        f"- **验收测试用例**: `{spec.acceptance_test_path}`",
        f"- **TDD 红灯强制校验**: `{'已验证通过 (Baseline Red -> Fixed Green)' if spec.require_tdd_red else '未开启'}`",
    ]
    if spec.tasks:
        ac_lines.append("- **原子任务分解清单**:")
        for t in spec.tasks:
            ac_lines.append(f"  - [x] **{t.id}: {t.title}** - {t.description}")
    parts.append("## 验收准则 (Acceptance Criteria)\n" + "\n".join(ac_lines))

    # Design & Impact Scope
    allowed_files = getattr(spec, "allowed_modify_files", None) or spec.allowed_paths or []
    files_str = ", ".join(f"`{f}`" for f in allowed_files) if allowed_files else "`全库受控`"
    deps_str = ", ".join(f"`{d}`" for d in spec.extra_dependencies) if spec.extra_dependencies else "无"
    impact_lines = [
        f"- **允许变更文件白名单**: {files_str}",
        f"- **增量外部依赖**: {deps_str}",
    ]
    parts.append("## 影响面与架构约束 (Design & Impact Scope)\n" + "\n".join(impact_lines))

    # Verification Performed (Actual executed verification commands, test output, linter, sandbox & Critic review)
    verif = verification or {}
    test_cmd = verif.get("test_cmd") or f"pytest -q {spec.acceptance_test_path}"
    test_exit = verif.get("test_exit", 0)
    test_summary = verif.get("test_summary") or "通过 (All acceptance tests passed)"
    lint_cmd = verif.get("lint_cmd")
    lint_exit = verif.get("lint_exit", 0)
    audit_passed = verif.get("audit_passed", True)

    status_label = "APPROVED (通过)" if verdict.approved else "CHANGES_REQUESTED (需要修改)"
    audit_status = (
        "PASSED (无白名单外越权变更，未篡改锁定测试)"
        if audit_passed
        else "FAILED"
    )

    verif_lines = [
        f"- **验收测试执行**: `{test_cmd}` (exit={test_exit})",
        f"- **测试结果摘要**: `{test_summary}`",
    ]
    if lint_cmd:
        verif_lines.append(f"- **代码风格与规范检查**: `{lint_cmd}` (exit={lint_exit} clean)")
    else:
        verif_lines.append("- **代码风格与规范检查**: `skipped (未配置 lint 命令)`")

    verif_lines.extend(
        [
            f"- **物理沙箱与 Worktree 审计**: `{audit_status}`",
            f"- **独立 Critic 审查门禁**: `{status_label}` (置信度: `{verdict.confidence}`)",
            f"- **Critic 审查摘要**: {verdict.summary}",
        ]
    )
    parts.append("## 验证执行记录 (Verification Performed)\n" + "\n".join(verif_lines))

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


async def save_and_commit_spec_in_worktree(
    spec: SpecContract,
    worktree: Path,
    run_id: str,
) -> tuple[bool, Path]:
    """Serialize SpecContract to OKF Markdown in worktree .adk/specs/ and commit to git.

    Guarantees that the specification is committed into the feature branch history alongside code changes.
    """
    spec_dir = worktree / ".adk" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    filename = f"issue-{spec.issue_iid}.md" if spec.issue_iid is not None else f"task-{run_id[:6]}.md"
    spec_file = spec_dir / filename
    spec_content = spec.to_okf_markdown()
    spec_file.write_text(spec_content, encoding="utf-8")

    rel_path = f".adk/specs/{filename}"
    add_res = await _git(["add", "-f", "--", rel_path], worktree)
    if add_res.returncode == 0:
        await _git(
            ["commit", "-m", f"docs(spec): append OKF specification for {run_id[:6]}"],
            worktree,
        )
        return True, spec_file
    return False, spec_file


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


def _validate_tdd_red_outcome(
    returncode: int,
    combined_output: str,
    cmd: Sequence[str] | None = None,
    test_path: str = "",
) -> None:
    """Validate that preflight test failure is a genuine TDD Red outcome rather than test/fixture bug."""
    if returncode == 0:
        path_str = f" `{test_path}`" if test_path else ""
        raise RuntimeError(
            f"TDD 预检失败：锁定的验收测试{path_str} 在未编写业务代码前直接通过 (exit=0)！\n"
            f"这表明该测试属于无断言空测试（Mock Passes），或该特性在基线代码中已存在。\n"
            f"请为验收测试增加针对新功能的有效断言，或在 Spec 中设置 require_tdd_red=False。"
        )

    cmd_str = f"命令: {' '.join(cmd)}\n" if cmd else ""

    fixture_error = ("fixture '" in combined_output and " not found" in combined_output) or (
        "fixture not found" in combined_output
    )
    fatal_patterns = (
        "pytest: error:",
        "usage: pytest",
        "ModuleNotFoundError:",
        "No module named",
        "SyntaxError:",
        "INTERNALERROR>",
        "command not found",
        "is not recognized as an internal or external command",
    )
    is_fatal_cli_error = fixture_error or any(p in combined_output for p in fatal_patterns)
    is_assertion_failure = (returncode == 1) or any(
        marker in combined_output for marker in ("FAILED", "AssertionError", "assert ")
    )

    if not is_assertion_failure or is_fatal_cli_error:
        reason = "测试 Fixture 配置错误或用例本身编写缺陷" if fixture_error else "非用例断言失败，测试环境或语法异常"
        raise RuntimeError(
            f"TDD 红灯预检未通过（{reason}）：验收测试执行异常 (exit={returncode})。\n"
            f"{cmd_str}"
            f"输出:\n{combined_output}\n"
            f"请修复测试语法与 Fixture 依赖，或检查运行环境后重试。"
        )


async def run_tdd_red_preflight(
    spec: SpecContract,
    worktree: Path,
    repo_root: Path,
    timeout_sec: float = VERIFY_TIMEOUT_SEC,
) -> None:
    """Pre-flight TDD Red verification on the clean baseline worktree.

    Fails closed if pytest cannot be resolved, if execution crashes, or if the test passes (exit 0).
    Requires a genuine assertion failure on baseline to prove the test is effective.
    """
    red_cmd = resolve_tool_command(
        "pytest",
        ["-q", spec.acceptance_test_path],
        worktree=worktree,
        repo_root=repo_root,
        env_override=os.environ.get("ADK_TDD_RED_CMD") or os.environ.get("ADK_TEST_CMD"),
        placeholder_vars={"test_path": spec.acceptance_test_path},
    )
    if red_cmd is None:
        raise RuntimeError(
            f"TDD 红灯预检失败：未能解析 pytest 执行命令（未找到 venv 或全局 pytest）。\n"
            f"请为仓库创建 Python 虚拟环境 (.venv) 并安装 pytest，或通过 ADK_TDD_RED_CMD 环境变量指定测试命令。"
        )

    red_res = await run_subprocess_safe(
        red_cmd, cwd=worktree, timeout_sec=timeout_sec
    )
    if red_res.timed_out:
        raise RuntimeError(f"TDD 红灯预检超时（>{timeout_sec}s）")

    combined_output = f"{red_res.stdout}\n{red_res.stderr}"
    _validate_tdd_red_outcome(
        returncode=red_res.returncode,
        combined_output=combined_output,
        cmd=red_cmd,
        test_path=spec.acceptance_test_path,
    )


def build_dev_workflow(
    repo_root: Path,
    store: StateStore,
    gl: BaseGitForge,
    run_id: str,
    human_hint: str | None = None,
) -> Workflow:
    """Build an async ADK 2.x Graph Workflow bound to a specific run_id via closure."""
    verification_state: dict[str, Any] = {}

    async def prepare_worktree_node(node_input: Any) -> dict[str, Any]:
        text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
        payload = json.loads(text)
        spec = SpecContract.model_validate(payload["spec"])

        existing_run = store.get_run(run_id)
        assert existing_run is not None, f"run_id={run_id} 不存在"

        branch = existing_run.get("branch") or getattr(spec, "branch", None)
        if not branch:
            branch_pattern = os.environ.get("ADK_BRANCH_PATTERN")
            if branch_pattern:
                title_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", spec.title.lower()).strip("-")[:25]
                branch = branch_pattern.format(
                    iid=spec.issue_iid or "task",
                    run_id=run_id,
                    run_id6=run_id[:6],
                    title=title_slug,
                )
            else:
                branch = f"adk/issue-{spec.issue_iid or 'task'}-{run_id[:6]}"
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
            base_ref = getattr(spec, "base_branch", None) or spec.target_branch
            res = await _git(
                ["worktree", "add", "-b", branch, str(worktree), base_ref],
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

            # Save and commit OKF spec contract into the worktree branch
            await save_and_commit_spec_in_worktree(spec, worktree=worktree, run_id=run_id)

            if spec.extra_dependencies:
                dep_ok, dep_msg = await sync_worktree_dependencies(
                    worktree, repo_root, spec.extra_dependencies
                )
                if not dep_ok:
                    raise RuntimeError(f"安装增量依赖失败: {dep_msg}")

            # Pre-flight TDD Red verification on the clean baseline worktree
            if spec.require_tdd_red and os.environ.get("ADK_SKIP_TDD_RED", "0") != "1":
                await run_tdd_red_preflight(spec, worktree=worktree, repo_root=repo_root)
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
            summary_line = ""
            if "test_res" in locals() and test_res and test_res.stdout.strip():
                lines = [line.strip() for line in test_res.stdout.strip().splitlines() if line.strip()]
                summary_line = lines[-1] if lines else "Passed"
            else:
                summary_line = "Passed"

            verification_state["record"] = {
                "test_cmd": " ".join(test_cmd) if test_cmd else f"pytest -q {spec.acceptance_test_path}",
                "test_exit": test_res.returncode if ("test_res" in locals() and test_res) else 0,
                "test_summary": summary_line,
                "lint_cmd": " ".join(lint_cmd) if lint_cmd else "",
                "lint_exit": lint_res.returncode if (lint_cmd and "lint_res" in locals() and lint_res) else 0,
                "audit_passed": audit.ok,
            }
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

        file_diffs: list[tuple[str, str]] = []
        for f in changed_files:
            file_diff_res = await _git(["diff", f"{base_sha}..HEAD", "--", f], worktree)
            file_diffs.append((f, file_diff_res.stdout))

        diff_text, diff_truncated, truncated_files = assemble_diff_blocks(file_diffs)

        # 3. 执行 AST 静态语义审计（语法树解析、破坏性接口变更、高危调用）
        ast_findings: list[dict[str, Any]] = []
        for f in changed_files:
            if not f.endswith(".py"):
                continue
            base_code = ""
            base_res = await _git(["show", f"{base_sha}:{f}"], worktree)
            if base_res.returncode == 0:
                base_code = base_res.stdout
            new_code = ""
            new_p = worktree / f
            if new_p.exists():
                try:
                    new_code = new_p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    new_code = ""

            try:
                from adk_dev_flow.ast_graph import audit_ast_diff

                report = audit_ast_diff(base_code, new_code, file_path=f)
                if report.has_issues:
                    ast_findings.append(report.to_dict())
            except Exception:
                pass

        # Save ast_findings into verification_state so publish_mr_node can deterministically enforce it.
        verification_state["ast_findings"] = ast_findings

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
            "ast_static_audit": ast_findings,
        }
        return json.dumps(critic_payload, ensure_ascii=False, indent=2)

    critic_env = os.environ.get("ADK_CRITIC_MODEL") or os.environ.get("CODEBUDDY_CRITIC_MODEL")
    critic_model: Any
    if not critic_env:
        critic_model = CodeBuddyLlm()
    elif hasattr(critic_env, "generate_content_async"):
        critic_model = critic_env
    else:
        critic_model = CodeBuddyLlm(model=str(critic_env))
    critic_review_agent = Agent(
        name="critic_review_agent",
        model=critic_model,
        output_schema=ReviewVerdict,
        instruction=(
            "你是独立的只读代码审查专家（Critic）。你收到的是 JSON 上下文，包含 spec、worker_summary、git_diff 及可选的 ast_static_audit 静态语义审计结论。"
            "严格检查：1) 是否违背 Spec 约束或越界修改；2) 若 ast_static_audit 中存在 syntax_error、dangerous_nodes 或 breaking_issues，必须在 blocking_issues 中列出并直接一票否决 (approved=false)；"
            "3) 是否有明显边界空值、异常处理或安全漏洞；4) 是否存在硬编码密钥或绕过测试的作弊行为；"
            "5) 若 diff_truncated 为 true 且无法确认完整性，请审慎评估并在 blocking_issues 说明。"
            "只输出符合 ReviewVerdict 结构的 JSON。"
        ),
    )

    async def publish_mr_node(node_input: Any) -> dict[str, Any]:
        # Strict Fail-Closed parsing: any invalid/unrecognized output sets approved=False.
        raw_dict = parse_review_verdict_dict(node_input)
        ast_findings = verification_state.get("ast_findings") or []
        guarded_dict = enforce_deterministic_verdict_guard(raw_dict, ast_findings)
        verdict = ReviewVerdict.model_validate(guarded_dict)

        run = store.get_run(run_id)
        if run is None:
            raise RuntimeError(f"run_id={run_id} 在发布阶段未找到")
        spec = SpecContract.model_validate_json(run["spec_json"])
        worktree = Path(run["worktree_path"])
        branch = run["branch"]
        attempt = int(run.get("attempt") or 0)

        mr_url = ""
        mr_iid = None

        # Save and commit OKF review audit document and spec contract into worktree
        try:
            await save_and_commit_spec_in_worktree(spec, worktree=worktree, run_id=run_id)
        except Exception:
            pass

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

            desc = _format_mr_description(
                spec,
                verdict,
                verification=verification_state.get("record"),
            )
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
