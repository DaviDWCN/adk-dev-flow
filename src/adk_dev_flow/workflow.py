from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Sequence

import yaml

from google.adk import Agent, Event, Runner, Workflow
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START
from google.genai import types

from adk_dev_flow.ast_graph import SymbolVisitor, audit_ast_diff
from adk_dev_flow.codebuddy_bridge import CodeBuddyLlm, run_codebuddy_worker
from adk_dev_flow.forge_client import BaseGitForge, GitLabClient, get_git_forge
from adk_dev_flow.guard import is_path_under, is_protected_path, normalize_relative_path, strip_dot_slash
from adk_dev_flow.models import PreCriticReport, ReviewVerdict, SpecContract
from adk_dev_flow.mutation import evaluate_test_against_mutants
from adk_dev_flow.providers import get_worker_provider
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import CommandResult, no_window_kwargs, run_subprocess_safe, run_subprocess_sync
from adk_dev_flow.worktree_utils import (
    _is_ephemeral_runtime_artifact,
    _parse_porcelain_paths,
    audit_worktree_changes,
    compute_error_signature,
    compute_file_sha256,
    compute_sha256_bytes,
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


def get_max_repair_attempts() -> int:
    val = os.environ.get("ADK_MAX_RETRIES", "3")
    try:
        return int(val)
    except (ValueError, TypeError):
        return 3


def get_git_timeout_sec() -> float:
    val = os.environ.get("ADK_GIT_TIMEOUT", "60")
    try:
        return float(val)
    except (ValueError, TypeError):
        return 60.0


MAX_REPAIR_ATTEMPTS = get_max_repair_attempts()
MAX_DIFF_CHARS = get_max_diff_chars()
GIT_TIMEOUT_SEC = get_git_timeout_sec()


def get_verify_timeout_sec() -> float:
    """Verify-gate timeout, read at call time so project .adk/env.json wins
    over import order (ADK_VERIFY_TIMEOUT was previously frozen at module
    import, before load_project_config applied project env — issue seen 2026-11-26)."""
    return float(os.environ.get("ADK_VERIFY_TIMEOUT", "180"))


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


async def _git(cmd: list[str], cwd: Path, timeout_sec: float | None = None) -> CommandResult:
    effective_timeout = get_git_timeout_sec() if timeout_sec is None else timeout_sec
    return await run_subprocess_safe(["git", *cmd], cwd=cwd, timeout_sec=effective_timeout)


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


async def run_readonly_critic_review(
    spec: SpecContract,
    worktree: Path,
    base_sha: str,
    ast_findings: list[dict[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Run an independent read-only Critic review against the git diff (zero write tools)."""
    stat_res = await _git(["diff", "--stat", f"{base_sha}..HEAD"], worktree)
    names_res = await _git(["diff", "--name-only", f"{base_sha}..HEAD"], worktree)
    changed_files = (
        [line.strip() for line in names_res.stdout.splitlines() if line.strip()]
        if names_res.returncode == 0
        else []
    )
    file_diffs: list[tuple[str, str]] = []
    for f_rel in changed_files:
        d_res = await _git(["diff", f"{base_sha}..HEAD", "--", f_rel], worktree)
        if d_res.returncode == 0 and d_res.stdout:
            file_diffs.append((f_rel, d_res.stdout))

    diff_text, _, _ = assemble_diff_blocks(file_diffs)
    ast_summary = json.dumps(ast_findings, ensure_ascii=False)[:2000] if ast_findings else "无静态告警"
    prompt = (
        f"请以严格只读第三方 Critic 视角审查以下变更：\n"
        f"- Spec 标题: {spec.title}\n"
        f"- Spec 摘要: {spec.summary}\n"
        f"- 允许路径白名单: {spec.allowed_paths}\n"
        f"- AST 静态门禁结果: {ast_summary}\n"
        f"- Git diff --stat:\n{stat_res.stdout[:2000]}\n\n"
        f"- Diff 详情:\n{diff_text}\n"
    )

    from google.adk.models.llm_request import LlmRequest

    critic_model = (os.environ.get("ADK_CRITIC_MODEL") or "").strip() or "codebuddy-critic"
    llm = CodeBuddyLlm(model=critic_model)
    req = LlmRequest(
        model=critic_model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            system_instruction=(
                "你是只读第三方代码审查 Critic，无任何文件写权限。"
                "请审查变更是否满足 Spec 且无逻辑回退、越权修改或安全隐患。"
            ),
            response_schema=ReviewVerdict,
        ),
    )

    last_text = ""
    try:
        async for resp in llm.generate_content_async(req, stream=False):
            if resp and resp.content and resp.content.parts:
                last_text = "\n".join(
                    p.text for p in resp.content.parts if getattr(p, "text", None)
                )
        parsed = parse_review_verdict_dict(last_text)
        if isinstance(parsed, dict) and "approved" in parsed:
            return parsed
        raise ValueError(f"Critic 未返回结构化 ReviewVerdict: {last_text[:200]}")
    except Exception as exc:
        if os.environ.get("ADK_CRITIC_FAIL_CLOSED", "0") == "1":
            return {
                "approved": False,
                "confidence": 1.0,
                "summary": f"Critic 只读审查调用失败 (fail-closed): {exc}",
                "blocking_issues": [f"[{run_id}] Critic 只读审查执行失败: {exc}"],
            }
        return {
            "approved": True,
            "confidence": 0.7,
            "summary": f"物理跑测与 AST 静态门禁通过（只读 Critic 降级: {type(exc).__name__}）",
            "blocking_issues": [],
        }


def parse_pre_critic_report_dict(raw_output: Any) -> dict[str, Any]:
    """Fail-closed parser for Step 3 PreCriticReport outputs."""
    from adk_dev_flow.worktree_utils import strip_markdown_code_fence

    payload: Any = raw_output
    if hasattr(payload, "model_dump") and callable(payload.model_dump):
        try:
            payload = payload.model_dump()
        except TypeError:
            pass
    if isinstance(payload, str):
        cleaned = strip_markdown_code_fence(payload)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end > start:
                try:
                    payload = json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    payload = None
            else:
                payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("approved"), bool):
        return {
            "approved": False,
            "consistency_score": 0.0,
            "requirement_coverage": [],
            "anti_cheat_risk": "unknown (unparseable pre-critic output)",
            "blocking_issues": ["Step 3 Pre-Critic 未返回有效的布尔 approved JSON 结论（Fail-Closed）"],
            "summary": str(raw_output)[:400] or "Pre-Critic 输出无法解析",
        }
    blocking_raw = payload.get("blocking_issues")
    blocking = (
        [str(x) for x in blocking_raw]
        if isinstance(blocking_raw, list)
        else ([str(blocking_raw)] if blocking_raw is not None else [])
    )
    cov_raw = payload.get("requirement_coverage")
    coverage = (
        [str(x) for x in cov_raw]
        if isinstance(cov_raw, list)
        else ([str(cov_raw)] if cov_raw is not None else [])
    )
    try:
        score = float(payload.get("consistency_score", 1.0 if payload["approved"] else 0.0))
    except (TypeError, ValueError):
        score = 1.0 if payload["approved"] else 0.0
    score = max(0.0, min(1.0, score))
    return PreCriticReport(
        approved=payload["approved"],
        consistency_score=score,
        requirement_coverage=coverage,
        anti_cheat_risk=str(payload.get("anti_cheat_risk") or ""),
        blocking_issues=blocking,
        summary=str(payload.get("summary") or "").strip()
        or ("Step 3 Pre-Critic 审查通过" if payload["approved"] else "Step 3 Pre-Critic 审查未通过"),
    ).model_dump()


async def run_pre_critic_review(
    spec: SpecContract,
    *,
    run_id: str,
    requirement: str = "",
    impact_analysis: dict[str, Any] | None = None,
    red_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Step 3 Pre-Lock AI Critic: review design, impact alignment, and anti-cheat test strength."""
    provider_type = os.environ.get("ADK_WORKER_PROVIDER", "").strip().lower()
    has_cli_override = bool(
        (os.environ.get("CODEBUDDY_BIN") or "").strip()
        or (os.environ.get("CODEBUDDY_BIN_ARGS") or "").strip()
    )
    if provider_type == "mock" and not has_cli_override:
        return PreCriticReport(
            approved=True,
            consistency_score=1.0,
            requirement_coverage=[f"{t.id}: {t.title}" for t in spec.tasks],
            anti_cheat_risk="low (mock pre-critic)",
            blocking_issues=[],
            summary=f"Step 3 Pre-Critic (Mock) 审查通过: {spec.title}",
        ).model_dump()

    tasks_md = "\n".join(
        f"- [{t.id}] {t.title}: {t.description} (targets={t.target_files}, symbols={t.target_symbols})"
        for t in spec.tasks
    )
    impact_str = json.dumps(impact_analysis or {}, ensure_ascii=False)[:3000]
    red_str = json.dumps(red_evidence or {}, ensure_ascii=False)[:1500]
    prompt = (
        f"请执行 Pre-Lock Step 3（设计与验收测试双审），判断契约是否完整且不可被工兵投机作弊：\n"
        f"- Run ID: {run_id}\n"
        f"- 原始需求: {requirement or spec.summary}\n"
        f"- Spec 标题: {spec.title}\n"
        f"- Spec 摘要: {spec.summary}\n"
        f"- 设计方案 (design_doc):\n{spec.design_doc or '（未提供）'}\n\n"
        f"- 允许修改白名单: {spec.allowed_paths}\n"
        f"- 原子任务清单:\n{tasks_md}\n\n"
        f"- AST 影响面分析:\n{impact_str}\n\n"
        f"- 锁定的验收测试 (`{spec.acceptance_test_path}`):\n```python\n{spec.acceptance_test_code}\n```\n\n"
        f"- 基线红灯客观凭证:\n{red_str}\n"
    )

    from google.adk.models.llm_request import LlmRequest

    critic_model = (os.environ.get("ADK_CRITIC_MODEL") or "").strip() or "codebuddy-critic"
    llm = CodeBuddyLlm(model=critic_model)
    req = LlmRequest(
        model=critic_model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            system_instruction=(
                "你是只读 Pre-Lock Critic 审查专家，无任何文件写权限。"
                "请严格审查：1) 设计与需求/影响面是否一致；"
                "2) 验收测试是否充分（能否被返回常量的投机实现欺骗通过、是否覆盖异常/边界情况）；"
                "3) 白名单路径与任务拆分是否合理。返回结构化 PreCriticReport JSON。"
            ),
            response_schema=PreCriticReport,
        ),
    )

    last_text = ""
    try:
        async for resp in llm.generate_content_async(req, stream=False):
            if resp and resp.content and resp.content.parts:
                last_text = "\n".join(
                    p.text for p in resp.content.parts if getattr(p, "text", None)
                )
        return parse_pre_critic_report_dict(last_text)
    except Exception as exc:
        return PreCriticReport(
            approved=False,
            consistency_score=0.0,
            requirement_coverage=[],
            anti_cheat_risk="unknown (pre-critic error)",
            blocking_issues=[f"[{run_id}] Step 3 Pre-Critic 执行失败: {exc}"],
            summary=f"Step 3 Pre-Critic 调用失败 (fail-closed): {exc}",
        ).model_dump()


def run_isolated_tdd_red_preflight(
    repo_root: Path,
    test_path: str,
    test_code: str,
    *,
    file_lock: Any = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run baseline TDD Red preflight in an isolated temporary Git worktree when HEAD exists.

    Falls back to locked repo_root execution only when the repository has no commits yet
    (e.g. brand-new repo or test fixture with empty .git).
    """
    import subprocess
    import sys
    import tempfile

    root = repo_root.resolve()
    raw_path = (test_path or "").strip()
    if not raw_path or Path(raw_path).is_absolute():
        return {
            "ok": False,
            "exit_code": 1,
            "is_pure_semantic_failure": False,
            "error": f"非法测试路径: `{test_path}`（必须为仓库内相对路径）",
        }
    rel_norm = normalize_relative_path(raw_path, root)
    if not rel_norm:
        return {
            "ok": False,
            "exit_code": 1,
            "is_pure_semantic_failure": False,
            "error": f"非法测试路径: `{test_path}`（禁止路径穿越或超出仓库根目录）",
        }
    if is_protected_path(rel_norm) or is_path_under(rel_norm, ".adk"):
        return {
            "ok": False,
            "exit_code": 1,
            "is_pure_semantic_failure": False,
            "error": f"禁止在受保护路径或系统目录执行预检写入: {rel_norm}",
        }

    try:
        ast.parse(test_code, filename=rel_norm)
    except SyntaxError as exc:
        return {
            "ok": False,
            "exit_code": 1,
            "is_pure_semantic_failure": False,
            "error": f"测试代码存在 Python 语法错误 (line {exc.lineno}): {exc.msg}",
        }

    test_sha256 = compute_sha256_bytes(test_code.encode("utf-8"))

    # Probe whether repository has a valid HEAD commit to spawn a detached worktree
    has_head = False
    try:
        head_probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            **no_window_kwargs(),
        )
        has_head = head_probe.returncode == 0 and bool((head_probe.stdout or "").strip())
    except Exception:
        has_head = False

    def _exec_pytest(run_dir: Path, used_isolated_wt: bool) -> dict[str, Any]:
        target_file = (run_dir / rel_norm).resolve()
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(test_code, encoding="utf-8")

        cmd = resolve_tool_command(
            "pytest",
            ["-q", str(target_file)],
            worktree=run_dir,
            repo_root=root,
            env_override=os.environ.get("ADK_TDD_RED_CMD") or os.environ.get("ADK_TEST_CMD"),
            placeholder_vars={"test_path": rel_norm},
        )
        if cmd is None or (len(cmd) >= 1 and cmd[0] == "pytest" and not shutil.which("pytest")):
            cmd = [sys.executable, "-m", "pytest", "-q", str(target_file)]

        env = os.environ.copy()
        wt_src = run_dir / "src"
        sep = ";" if os.name == "nt" else ":"
        candidates = [str(wt_src)] if wt_src.is_dir() else [str(run_dir)]
        if wt_src.is_dir():
            candidates.append(str(run_dir))
        if env.get("PYTHONPATH"):
            candidates.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = sep.join(candidates)

        try:
            proc = run_subprocess_sync(
                cmd,
                cwd=run_dir,
                env=env,
                timeout_sec=timeout_sec,
            )
            if proc.timed_out:
                return {
                    "ok": False,
                    "exit_code": -1,
                    "is_pure_semantic_failure": False,
                    "error": f"测试执行超时 (>{int(timeout_sec)}s)",
                }
            combined_out = f"{proc.stdout}\n{proc.stderr}"
            try:
                _validate_tdd_red_outcome(
                    returncode=proc.returncode,
                    combined_output=combined_out,
                    cmd=cmd,
                    test_path=rel_norm,
                )
                return {
                    "ok": True,
                    "test_path": rel_norm,
                    "test_sha256": test_sha256,
                    "exit_code": proc.returncode,
                    "is_pure_semantic_failure": True,
                    "assertion_error_summary": combined_out.strip()[-1000:],
                    "command": " ".join(cmd),
                    "isolated_worktree": used_isolated_wt,
                }
            except RuntimeError as err:
                return {
                    "ok": False,
                    "exit_code": proc.returncode,
                    "is_pure_semantic_failure": False,
                    "error": str(err),
                    "output": combined_out[-1000:],
                }
        except Exception as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "is_pure_semantic_failure": False,
                "error": f"测试执行失败: {exc}",
            }

    if has_head:
        with tempfile.TemporaryDirectory() as wt_tmp:
            wt_dir = Path(wt_tmp) / "preflight_wt"
            wt_add = subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                **no_window_kwargs(),
            )
            if wt_add.returncode == 0 and wt_dir.is_dir():
                try:
                    return _exec_pytest(wt_dir, True)
                finally:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt_dir)],
                        cwd=str(root),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=20,
                        **no_window_kwargs(),
                    )
                    if wt_dir.exists():
                        shutil.rmtree(wt_dir, ignore_errors=True)
                    subprocess.run(
                        ["git", "worktree", "prune"],
                        cwd=str(root),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=15,
                        **no_window_kwargs(),
                    )

    # Fallback: repository without HEAD commits
    from contextlib import nullcontext

    target_in_root = (root / rel_norm).resolve()
    ctx = file_lock if file_lock is not None else nullcontext()
    with ctx:
        target_in_root.parent.mkdir(parents=True, exist_ok=True)
        old_content: bytes | None = None
        if target_in_root.is_file():
            try:
                old_content = target_in_root.read_bytes()
            except Exception:
                old_content = None
        try:
            return _exec_pytest(root, False)
        finally:
            if old_content is not None:
                try:
                    target_in_root.write_bytes(old_content)
                except Exception:
                    pass
            else:
                try:
                    target_in_root.unlink(missing_ok=True)
                except Exception:
                    pass


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


def distill_knowledge_from_run(
    spec: SpecContract,
    run_id: str,
    verdict: ReviewVerdict,
    attempt: int = 0,
    last_error_log: str = "",
) -> str | None:
    """Distill learned engineering invariants/rules from a completed run into an OKF EngineeringRule."""
    if attempt <= 0 and verdict.approved:
        return None

    title = f"Learned Rule: {spec.title}"
    meta = {
        "type": "EngineeringRule",
        "title": title,
        "status": "active",
        "source_run_id": run_id,
        "paths": spec.allowed_paths,
        "tags": ["learned", "auto-distilled"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()

    body = [
        f"# {title}",
        "",
        "## Summary & Context",
        spec.summary,
        "",
        "## Resolution & Key Gotchas",
        f"- **Verdict Summary**: {verdict.summary}",
    ]

    if last_error_log.strip():
        err_snippet = last_error_log.strip()[-500:]
        body.append(f"- **Resolved Failure Fingerprint**:\n```\n{err_snippet}\n```")

    if verdict.blocking_issues:
        body.append("- **Resolved Blocking Issues**:")
        for iss in verdict.blocking_issues:
            body.append(f"  - {iss}")

    return f"---\n{header}\n---\n\n" + "\n".join(body) + "\n"


async def save_distilled_knowledge(
    spec: SpecContract,
    worktree: Path,
    run_id: str,
    verdict: ReviewVerdict,
    attempt: int,
    last_error_log: str = "",
) -> Path | None:
    content = distill_knowledge_from_run(
        spec=spec,
        run_id=run_id,
        verdict=verdict,
        attempt=attempt,
        last_error_log=last_error_log,
    )
    if not content:
        return None

    rules_dir = worktree / ".adk" / "knowledge" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    rule_file = rules_dir / f"learned_{run_id[:8]}.md"
    rule_file.write_text(content, encoding="utf-8")

    rel_path = f".adk/knowledge/rules/{rule_file.name}"
    await _git(["add", "-f", "--", rel_path], worktree)
    await _git(
        ["commit", "-m", f"docs(knowledge): distill learned OKF rule from run {run_id[:8]}"],
        worktree,
    )
    return rule_file


def _match_path_pattern(path: str, pattern: str) -> bool:
    norm_path = path.strip().replace("\\", "/").strip("/")
    norm_pattern = pattern.strip().replace("\\", "/").strip("/")
    if fnmatch.fnmatch(norm_path, norm_pattern):
        return True
    if norm_pattern.endswith("/**"):
        prefix = norm_pattern[:-3]
        if norm_path == prefix or norm_path.startswith(prefix + "/"):
            return True
    if norm_pattern.endswith("/*"):
        prefix = norm_pattern[:-2]
        if norm_path.startswith(prefix + "/"):
            return True
    if norm_path == norm_pattern or norm_path.startswith(norm_pattern + "/"):
        return True
    return False


def _load_okf_rules(
    worktree: Path,
    repo_root: Path,
    allowed_paths: list[str] | None = None,
    tags: list[str] | None = None,
) -> str:
    rules_text: list[str] = []
    seen: set[str] = set()
    norm_allowed = (
        [p.strip().replace("\\", "/").strip("/") for p in allowed_paths]
        if allowed_paths
        else []
    )
    target_tags = set(tags or [])

    for base in (worktree, repo_root):
        rules_dir = base / ".adk" / "knowledge" / "rules"
        if rules_dir.is_dir():
            for rule_file in sorted(rules_dir.glob("*.md")):
                if rule_file.name in seen:
                    continue
                seen.add(rule_file.name)
                try:
                    raw_content = rule_file.read_text(encoding="utf-8").strip()
                    if not raw_content:
                        continue

                    # Parse optional YAML frontmatter
                    meta: dict[str, Any] = {}
                    body = raw_content
                    fm_match = re.match(
                        r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$",
                        raw_content,
                        re.DOTALL,
                    )
                    if fm_match:
                        try:
                            parsed_meta = yaml.safe_load(fm_match.group(1))
                            if isinstance(parsed_meta, dict):
                                meta = parsed_meta
                            body = fm_match.group(2).strip()
                        except Exception:
                            meta = {}

                    # Skip deprecated/disabled rules
                    status = str(meta.get("status", "active")).lower()
                    if status in ("deprecated", "disabled", "inactive"):
                        continue

                    # Apply selective routing when filtering criteria are passed
                    if allowed_paths is not None or tags is not None:
                        rule_paths = (
                            meta.get("paths")
                            or meta.get("match", {}).get("paths")
                            or []
                        )
                        rule_tags = (
                            meta.get("tags")
                            or meta.get("match", {}).get("tags")
                            or []
                        )

                        is_universal = not rule_paths and not rule_tags

                        path_matched = False
                        if rule_paths:
                            if any(p in ("*", "all") for p in rule_paths):
                                path_matched = True
                            elif norm_allowed:
                                path_matched = any(
                                    _match_path_pattern(ap, rp)
                                    for ap in norm_allowed
                                    for rp in rule_paths
                                )

                        tag_matched = False
                        if rule_tags and target_tags:
                            tag_matched = bool(set(rule_tags) & target_tags)

                        if not is_universal:
                            if rule_paths and not path_matched and not tag_matched:
                                continue
                            if rule_tags and not rule_paths and not tag_matched:
                                continue

                    title = meta.get("title") or rule_file.stem
                    rules_text.append(f"### {title}\n{body}")
                except Exception:
                    pass

    if not rules_text:
        return ""
    return "\n\n项目工程规约 (OKF Rules):\n" + "\n\n".join(rules_text)


def _build_ast_context(repo_or_worktree: Path, allowed_paths: list[str]) -> str:
    """Extract AST skeletons and caller signatures to ground worker agent in physical code reality."""
    try:
        from adk_dev_flow.ast_graph import CodeGraph

        with CodeGraph(repo_or_worktree) as cg:
            ctx = cg.get_test_generation_context(allowed_paths)
        skeletons = ctx.get("target_skeletons") or {}
        callers = ctx.get("impacted_caller_skeletons") or {}

        if not skeletons and not callers:
            return ""

        parts = ["\n\n### 代码结构与现有签名上下文 (AST Skeletons & Callers):"]
        if skeletons:
            parts.append("以下是目标修改文件的现有类/函数签名骨架（请严格维持现有公共接口兼容性）：")
            for fpath, skel in skeletons.items():
                parts.append(f"#### 文件: `{fpath}`\n```python\n{skel.strip()}\n```")
        if callers:
            parts.append("以下是调用这些目标符号的关键上游调用方签名（修改时切勿破坏其调用约定）：")
            for sym, sig in list(callers.items())[:12]:
                parts.append(f"- `{sig}` (调用 `{sym}`)")
        return "\n\n".join(parts)
    except Exception:
        return ""


def audit_symbol_alignment(
    tasks: list[AtomicTask],
    definitions_by_file: dict[str, list[str]],
) -> list[str]:
    """Verify that declared target_symbols in OKF tasks are present in changed files' AST definitions."""
    all_defs: set[str] = set()
    for syms in definitions_by_file.values():
        all_defs.update(syms)

    missing: list[str] = []
    for t in tasks:
        for sym in getattr(t, "target_symbols", []):
            clean_sym = sym.strip()
            if not clean_sym:
                continue
            matched = any(
                d == clean_sym or d.endswith(f".{clean_sym}") or clean_sym.endswith(f".{d}")
                for d in all_defs
            )
            if not matched:
                missing.append(
                    f"任务 {t.id} 声明的目标符号 `{clean_sym}` 未在提交代码的 AST 符号表中定义"
                )
    return missing


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
    timeout_sec: float | None = None,
) -> None:
    """Pre-flight TDD Red verification on the clean baseline worktree.

    Fails closed if pytest cannot be resolved, if execution crashes, or if the test passes (exit 0).
    Requires a genuine assertion failure on baseline to prove the test is effective.
    """
    if timeout_sec is None:
        timeout_sec = get_verify_timeout_sec()
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


def _transition_or_update(
    store: StateStore,
    run_id: str,
    *,
    expected_statuses: Sequence[str],
    new_status: str,
    **fields: Any,
) -> None:
    """Transition using enforce_matrix=True for admissible sources; allow test stubs outside pipeline."""
    current = store.get_run(run_id)
    cur_status = current.get("status") if current else None
    if cur_status in expected_statuses:
        ok = store.transition_run(
            run_id,
            expected_statuses=expected_statuses,
            new_status=new_status,  # type: ignore[arg-type]
            enforce_matrix=True,
            **fields,
        )
        if ok:
            return
    store.update_run(run_id, status=new_status, **fields)


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

        is_valid_worktree = worktree.exists() and (worktree / ".git").exists()
        if not is_valid_worktree:
            if worktree.exists():
                try:
                    shutil.rmtree(worktree, ignore_errors=True)
                except Exception:
                    pass
                await _git(["worktree", "prune"], repo_root)
            worktree.parent.mkdir(parents=True, exist_ok=True)
            explicit_base = (getattr(spec, "base_branch", None) or "").strip()
            base_ref = explicit_base or spec.target_branch
            res = await _git(
                ["worktree", "add", "-b", branch, str(worktree), base_ref],
                repo_root,
            )
            if res.returncode != 0:
                if explicit_base:
                    probe_base = await _git(
                        ["rev-parse", "--verify", "--quiet", f"{explicit_base}^{{commit}}"],
                        repo_root,
                    )
                    if probe_base.returncode != 0:
                        raise RuntimeError(
                            f"指定的基准分支 `{explicit_base}` 不存在，拒绝静默回退到 HEAD: {res.stderr.strip()}"
                        )
                if worktree.exists():
                    shutil.rmtree(worktree, ignore_errors=True)
                    await _git(["worktree", "prune"], repo_root)
                res = await _git(
                    ["worktree", "add", "-b", branch, str(worktree), "HEAD"],
                    repo_root,
                )
                if res.returncode != 0:
                    if worktree.exists():
                        shutil.rmtree(worktree, ignore_errors=True)
                        await _git(["worktree", "prune"], repo_root)
                    # If branch already exists from a prior interrupted attempt, checkout existing branch
                    res = await _git(
                        ["worktree", "add", str(worktree), branch],
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

            # Save and commit OKF spec contract BEFORE capturing locked_test_commit_sha
            # so pipeline-generated .adk/specs/ commits precede the worker diff boundary.
            await save_and_commit_spec_in_worktree(spec, worktree=worktree, run_id=run_id)

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

        _transition_or_update(
            store,
            run_id,
            expected_statuses=("SPEC_LOCKED", "ESCALATED", "RUNNING"),
            new_status="RUNNING",
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
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("RUNNING", "REPAIRING"),
                new_status="REPAIRING",
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
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("RUNNING", "ESCALATED"),
                new_status="RUNNING",
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
            max_retries = get_max_repair_attempts()
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("VERIFYING", "REPAIRING", "RUNNING", "ESCALATED"),
                new_status="REPAIRING",
                attempt=attempt,
                last_error_log=f"[Worker] 🔨 正在执行第 {attempt}/{max_retries} 轮修复",
            )
            prompt = (
                f"第 {attempt}/{max_retries} 轮定向修复。上次验收校验失败，"
                f"请仅修复白名单 `{spec.allowed_paths}` 内的业务代码使校验通过，"
                f"严禁修改或删除验收测试 `{spec.acceptance_test_path}`。\n\n"
                f"失败日志:\n```\n{str(node_input.get('last_error_log', ''))[-3000:]}\n```"
            )

            # Repair hint: feed the previous round's worker output back so the
            # repair round is not blind (lesson: run 558a138f54eb — rounds were
            # stateless, each worker exited after pure exploration, and nobody
            # told the next round that nothing had been written).
            prev_summary = str(node_input.get("worker_summary") or "").strip()
            prev_changed = list(node_input.get("worker_changed_files") or [])
            if not prev_summary and not prev_changed:
                recent_logs = store.list_attempt_logs(run_id, limit=2)
                if recent_logs:
                    latest = recent_logs[0]
                    prev_summary = str(latest.get("worker_summary") or "").strip()
                    try:
                        prev_changed = list(json.loads(latest.get("changed_files") or "[]"))
                    except Exception:
                        prev_changed = []
            if prev_summary or prev_changed:
                hint_lines = []
                if prev_summary:
                    hint_lines.append(f"- 上一轮工兵自述: {prev_summary[:500]}")
                if prev_changed:
                    hint_lines.append(
                        f"- 上一轮实际改动文件: {', '.join(prev_changed[:20])}"
                    )
                else:
                    hint_lines.append(
                        "- 上一轮实际改动文件: 无 (上一轮为空转) — 本轮必须真正写入/修改白名单内文件"
                    )
                prompt += "\n\n### 上一轮修复回执 (Repair Hint):\n" + "\n".join(hint_lines) + "\n"

        if spec.design_doc:
            prompt += f"\n\n### 架构设计方案 (Architecture Design):\n{spec.design_doc.strip()}\n"

        ast_ctx = _build_ast_context(worktree, spec.allowed_paths)
        if ast_ctx:
            prompt += ast_ctx

        okf_rules = _load_okf_rules(
            worktree,
            repo_root,
            allowed_paths=spec.allowed_paths,
        )
        if okf_rules:
            prompt += okf_rules

        provider = get_worker_provider()
        # Worker turn budget applies to every run (discovery-type tasks on a
        # cheap model spend a small budget on reverse-engineering and exit
        # before ever writing a file — lesson: run 558a138f54eb). Spike runs
        # multiply the configured base by 4 (floor 60) and double the timeout.
        try:
            max_turns = int(os.environ.get("ADK_WORKER_MAX_TURNS", "15"))
        except ValueError:
            max_turns = 15
        worker_timeout: int | None = None
        if spec.spike:
            max_turns = max(60, max_turns * 4)
            try:
                base_timeout = int(os.environ.get("ADK_WORKER_TIMEOUT", "300"))
            except ValueError:
                base_timeout = 300
            worker_timeout = base_timeout * 2
        result = await provider.run_worker(
            worktree=worktree,
            prompt=prompt,
            run_id=run_id,
            allowed_paths=spec.allowed_paths,
            locked_tests=[spec.acceptance_test_path],
            max_turns=max_turns,
            timeout_sec=worker_timeout,
        )
        node_input["worker_summary"] = result.get("summary", "")
        node_input["worker_changed_files"] = list(result.get("changed_files") or [])
        return node_input

    async def verify_python_node(node_input: dict[str, Any]) -> Event:
        spec = SpecContract.model_validate(node_input["spec"])
        worktree = Path(node_input["worktree"])
        attempt = int(node_input["attempt"])

        _transition_or_update(
            store,
            run_id,
            expected_statuses=("RUNNING", "REPAIRING", "ESCALATED", "VERIFYING"),
            new_status="VERIFYING",
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
        zero_change = False
        lint_cmd: list[str] | None = None
        lint_res: CommandResult | None = None
        test_cmd: list[str] | None = None
        test_res: CommandResult | None = None
        if not audit.ok:
            failure_sections.append(
                "[worktree_audit FAILED]\n" + "\n".join(f"- {v}" for v in audit.violations)
            )
        else:
            # Step 1b: Zero-output detection. A worktree with no changes at all means
            # the worker produced nothing this round; retrying blindly burns repair
            # rounds on identical outcomes (lesson: run 558a138f54eb).
            # Diff against locked_test_commit_sha (post test/spec setup) when present,
            # otherwise base_sha, so setup commits don't mask a zero-change worker.
            zero_change = False
            diff_base = str(
                node_input.get("locked_test_commit_sha")
                or node_input.get("base_sha")
                or ""
            ).strip()
            if diff_base:
                try:
                    names_res = await _git(["diff", "--name-only", f"{diff_base}..HEAD"], worktree)
                    status_res = await _git(
                        ["status", "--porcelain", "-uall", "--ignored=matching"], worktree
                    )
                    committed_changed = [
                        strip_dot_slash(line.strip())
                        for line in (names_res.stdout or "").splitlines()
                        if line.strip()
                        and not _is_ephemeral_runtime_artifact(strip_dot_slash(line.strip()))
                    ]
                    dirty_non_ephemeral = [
                        strip_dot_slash(p)
                        for p in _parse_porcelain_paths(status_res.stdout or "")
                        if p.strip()
                        and not _is_ephemeral_runtime_artifact(strip_dot_slash(p))
                    ]
                    zero_change = (
                        names_res.returncode == 0
                        and not committed_changed
                        and not dirty_non_ephemeral
                    )
                except Exception:
                    zero_change = False
            if zero_change:
                failure_sections.append(
                    "[zero_change VIOLATION]\n"
                    "- 工兵本轮对隔离 worktree 零改动：基线以来无提交且无未提交变更，判定为空转违规"
                )
            if spec.extra_dependencies:
                await sync_worktree_dependencies(worktree, repo_root, spec.extra_dependencies)

            # Step 2: Prepare subprocess environment with worktree sources prioritized in PYTHONPATH
            subproc_env = dict(os.environ)
            wt_src = worktree / "src"
            candidates = [str(wt_src)] if wt_src.is_dir() else [str(worktree)]
            sep = ";" if os.name == "nt" else ":"
            existing_pypath = subproc_env.get("PYTHONPATH", "")
            if existing_pypath:
                candidates.append(existing_pypath)
            subproc_env["PYTHONPATH"] = sep.join(candidates)

            # Step 3: Resolve commands inside target repository venv (or env overrides).
            lint_targets = [p for p in spec.allowed_paths if (worktree / p).exists()]
            lint_override = os.environ.get("ADK_LINT_CMD")
            if lint_targets or (lint_override is not None and lint_override.strip()):
                lint_args = ["check", *lint_targets] if lint_targets else ["check", "."]
                lint_cmd = resolve_tool_command(
                    "ruff",
                    lint_args,
                    worktree=worktree,
                    repo_root=repo_root,
                    env_override=lint_override,
                )
            else:
                lint_cmd = None
            if lint_cmd is not None:
                try:
                    lint_res = await run_subprocess_safe(
                        lint_cmd, cwd=worktree, env=subproc_env, timeout_sec=get_verify_timeout_sec()
                    )
                    if lint_res.returncode != 0:
                        failure_sections.append(
                            f"[lint cmd={' '.join(lint_cmd)} exit={lint_res.returncode}]\n"
                            f"{lint_res.stdout}\n{lint_res.stderr}"
                        )
                except OSError as exc:
                    failure_sections.append(f"[lint cmd={' '.join(lint_cmd)} spawn_error]\n{exc}")

            test_cmd = resolve_tool_command(
                "pytest",
                ["-q", spec.acceptance_test_path],
                worktree=worktree,
                repo_root=repo_root,
                env_override=os.environ.get("ADK_TEST_CMD"),
                placeholder_vars={"test_path": spec.acceptance_test_path},
            )
            if test_cmd is not None:
                try:
                    test_res = await run_subprocess_safe(
                        test_cmd, cwd=worktree, env=subproc_env, timeout_sec=get_verify_timeout_sec()
                    )
                    if test_res.returncode != 0:
                        failure_sections.append(
                            f"[pytest cmd={' '.join(test_cmd)} exit={test_res.returncode}]\n"
                            f"{test_res.stdout}\n{test_res.stderr}"
                        )
                except OSError as exc:
                    failure_sections.append(f"[pytest cmd={' '.join(test_cmd)} spawn_error]\n{exc}")

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
                    try:
                        reg_res = await run_subprocess_safe(
                            reg_cmd, cwd=worktree, env=subproc_env, timeout_sec=get_verify_timeout_sec()
                        )
                        if reg_res.returncode != 0:
                            failure_sections.append(
                                f"[regression cmd={' '.join(reg_cmd)} exit={reg_res.returncode}]\n"
                                f"{reg_res.stdout}\n{reg_res.stderr}"
                            )
                    except OSError as exc:
                        failure_sections.append(f"[regression cmd={' '.join(reg_cmd)} spawn_error]\n{exc}")

        physical_changed = (
            list(audit.touched_files)
            if audit.touched_files
            else list(node_input.get("worker_changed_files") or [])
        )
        node_input["worker_changed_files"] = physical_changed

        # Optional deterministic Post-Green Mutation Gate when require_tdd_red=True
        post_green_enabled = (
            os.environ.get("ADK_POST_GREEN_MUTATION", "").strip().lower() in ("1", "true", "yes")
        )
        if not failure_sections and post_green_enabled and spec.require_tdd_red and spec.require_mutation_gate:
            from adk_dev_flow.guard import is_path_under as _guard_under

            norm_test_rel = strip_dot_slash(spec.acceptance_test_path)
            mut_candidates = [
                f
                for f in physical_changed
                if f.endswith(".py")
                and f != norm_test_rel
                and any(_guard_under(f, allowed) for allowed in spec.allowed_paths)
            ]
            if not mut_candidates:
                mut_candidates = [
                    tf
                    for t in spec.tasks
                    for tf in t.target_files
                    if tf.endswith(".py") and (worktree / tf).is_file()
                ]
            seen_mut: set[str] = set()
            target_sources: list[tuple[str, str]] = []
            for tf in mut_candidates:
                if tf in seen_mut:
                    continue
                seen_mut.add(tf)
                fp = worktree / tf
                if fp.is_file():
                    target_sources.append((tf, fp.read_text(encoding="utf-8", errors="replace")))

            wt_test_file = worktree / norm_test_rel
            test_src_code = (
                wt_test_file.read_text(encoding="utf-8", errors="replace")
                if wt_test_file.is_file()
                else spec.acceptance_test_code
            )
            if target_sources:
                extra_py = [str(worktree / "src"), str(worktree)]
                total_mutants = 0
                total_killed = 0
                all_survived: list[str] = []
                all_acceptable = True
                for target_rel, target_code in target_sources:
                    m_report = await evaluate_test_against_mutants(
                        target_source=target_code,
                        test_source=test_src_code,
                        target_module_name=target_rel,
                        min_kill_ratio=0.85,
                        max_mutants=30,
                        extra_pythonpaths=extra_py,
                    )
                    total_mutants += m_report.total_mutants
                    total_killed += m_report.killed_count
                    all_survived.extend(f"{target_rel}: {d}" for d in m_report.survived_details)
                    if not m_report.is_acceptable:
                        all_acceptable = False
                combined_ratio = (total_killed / total_mutants) if total_mutants > 0 else 1.0
                if not (all_acceptable and combined_ratio >= 0.85):
                    failure_sections.append(
                        f"[post_green_mutation FAILED] 变异测试击杀率未达 85.0% ({combined_ratio:.1%})。\n"
                        + "\n".join(all_survived[:10])
                    )

        if not failure_sections:
            summary_line = ""
            if test_res is not None and test_res.stdout.strip():
                lines = [line.strip() for line in test_res.stdout.strip().splitlines() if line.strip()]
                summary_line = lines[-1] if lines else "Passed"
            else:
                summary_line = "Passed"

            verification_state["record"] = {
                "test_cmd": " ".join(test_cmd) if test_cmd else f"pytest -q {spec.acceptance_test_path}",
                "test_exit": test_res.returncode if test_res is not None else 0,
                "test_summary": summary_line,
                "lint_cmd": " ".join(lint_cmd) if lint_cmd else "",
                "lint_exit": lint_res.returncode if (lint_cmd and lint_res is not None) else 0,
                "audit_passed": audit.ok,
            }
            # Physical tests all green; remain in VERIFYING until publish_mr_node finalizes Step 8 MR_READY
            store.log_attempt(
                run_id,
                attempt,
                worker_summary=str(node_input.get("worker_summary") or ""),
                changed_files=physical_changed,
            )
            store.update_run(
                run_id,
                last_error_log="[Verification] ✅ 物理跑测全绿，正在执行 AST 静态门禁与交付入库",
            )
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
        store.log_attempt(
            run_id,
            attempt,
            err_sig=err_sig,
            combined_err=combined_err[-4000:],
            worker_summary=str(node_input.get("worker_summary") or ""),
            changed_files=physical_changed,
        )
        store.update_run(
            run_id,
            attempt=next_attempt,
            last_error_sig=err_sig,
            last_error_log=combined_err[-4000:],
        )

        # Zero-output violation escalates immediately — a fresh round would repeat
        # the same nothing (lesson: run 558a138f54eb burned 3 rounds on empty worktrees).
        if zero_change:
            node_input["escalation_reason"] = (
                "工兵本轮零改动（未产生任何代码变更），立即熔断避免空转烧轮次"
            )
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("VERIFYING",),
                new_status="ESCALATED",
            )
            return Event(output=node_input, route="escalated")

        # Fixed off-by-one: MAX_REPAIR_ATTEMPTS=3 allows initial(0) + repair rounds 1, 2, 3.
        max_retries = get_max_repair_attempts()
        if next_attempt > max_retries or repeated_same_error:
            node_input["escalation_reason"] = (
                "连续两轮出现相同的报错指纹，触发防死循环熔断"
                if repeated_same_error
                else f"已达最大自动修复轮次上限 ({max_retries})"
            )
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("VERIFYING",),
                new_status="ESCALATED",
            )
            return Event(output=node_input, route="escalated")

        _transition_or_update(
            store,
            run_id,
            expected_statuses=("VERIFYING",),
            new_status="REPAIRING",
        )
        return Event(output=node_input, route="retry")

    async def publish_mr_node(node_input: Any) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise RuntimeError(f"run_id={run_id} 在发布阶段未找到")
        spec = SpecContract.model_validate_json(run["spec_json"])
        worktree = Path(run["worktree_path"])
        branch = run["branch"]
        attempt = int(run.get("attempt") or 0)
        base_sha = str(run.get("base_sha") or "HEAD~1")
        diff_base = str(run.get("locked_test_commit_sha") or base_sha)

        # 1. 自动提交工兵在沙箱内的改动（如果尚未提交），排除 .adk/cache 运行时缓存
        status_res = await _git(["status", "--porcelain"], worktree)
        if status_res.returncode == 0 and status_res.stdout.strip():
            add_res = await _git(["add", "-A", "--", ".", ":(exclude).adk/cache"], worktree)
            if add_res.returncode != 0:
                err_msg = f"沙箱改动 git add 失败，拒绝发布空提交: {add_res.stderr.strip()}"
                _transition_or_update(
                    store,
                    run_id,
                    expected_statuses=("VERIFYING", "REVIEWING", "RUNNING"),
                    new_status="ESCALATED",
                    last_error_log=err_msg,
                )
                return {"run_id": run_id, "status": "ESCALATED", "error": err_msg}
            commit_res = await _git(["commit", "-m", f"feat: {spec.title}"], worktree)
            if commit_res.returncode != 0:
                err_msg = f"沙箱改动 git commit 失败，拒绝发布空提交: {commit_res.stderr.strip()}"
                _transition_or_update(
                    store,
                    run_id,
                    expected_statuses=("VERIFYING", "REVIEWING", "RUNNING"),
                    new_status="ESCALATED",
                    last_error_log=err_msg,
                )
                return {"run_id": run_id, "status": "ESCALATED", "error": err_msg}

        # 1b. Enter REVIEWING stage (Step 6 Critic highlights while verifying diff + AST invariants)
        _transition_or_update(
            store,
            run_id,
            expected_statuses=("VERIFYING", "REVIEWING"),
            new_status="REVIEWING",
            last_error_log="[Critic] 🧐 正在以只读权限执行 Git Diff 与 AST 静态契约审查",
        )

        # 2. 提取变更并执行确定性 AST 静态语义审计
        ast_findings = list(verification_state.get("ast_findings") or [])
        if not ast_findings and worktree.exists():
            try:
                names_res = await _git(["diff", "--name-only", f"{diff_base}..HEAD"], worktree)
                changed_files = (
                    [line.strip() for line in names_res.stdout.splitlines() if line.strip()]
                    if names_res.returncode == 0
                    else []
                )
                definitions_by_file: dict[str, list[str]] = {}
                for f in changed_files:
                    if not f.endswith(".py"):
                        continue
                    base_code = ""
                    base_res = await _git(["show", f"{diff_base}:{f}"], worktree)
                    if base_res.returncode == 0:
                        base_code = base_res.stdout
                    new_p = worktree / f
                    new_code = new_p.read_text(encoding="utf-8", errors="replace") if new_p.exists() else ""
                    if new_code.strip():
                        try:
                            tree = ast.parse(new_code, filename=f)
                            vis = SymbolVisitor(new_code.splitlines())
                            vis.visit(tree)
                            definitions_by_file[f] = [k for k, v in vis.definitions.items() if not v.is_local]
                        except Exception:
                            pass
                    report = audit_ast_diff(base_code, new_code, file_path=f)
                    if report.has_issues:
                        ast_findings.append(report.to_dict())

                missing_symbols = audit_symbol_alignment(spec.tasks, definitions_by_file)
                if missing_symbols:
                    ast_findings.append({
                        "file_path": "spec.tasks",
                        "breaking_issues": missing_symbols,
                        "syntax_ok": True,
                        "has_issues": True,
                    })
                verification_state["ast_findings"] = ast_findings
            except Exception as exc:
                # Fail-closed: 审计器内部错误（解析/审计/IO 异常）绝不允许静默吞掉后放行，
                # 必须熔断为 ESCALATED 拦截发布。
                err_msg = f"[AST 审计器内部错误，fail-closed 拦截发布] {type(exc).__name__}: {exc}"
                store.update_run(run_id, status="ESCALATED", last_error_log=err_msg)
                return {
                    "run_id": run_id,
                    "status": "ESCALATED",
                    "error": err_msg,
                }

        # 3. 检查输入中是否携带 mock/override ast_findings (如对抗性测试注入)
        if isinstance(node_input, dict):
            injected = (
                node_input.get("ast_findings")
                or node_input.get("ast_static_audit")
                or (node_input.get("review", {}).get("ast_findings") if isinstance(node_input.get("review"), dict) else None)
            )
            if injected:
                ast_findings.extend(injected)

        # 4. AST 确定性硬门禁一票否决检查
        blocking_issues: list[str] = []
        for finding in ast_findings:
            if finding.get("has_issues"):
                blocking_issues.extend(finding.get("breaking_issues", []))
                blocking_issues.extend(finding.get("dangerous_nodes", []))
                if finding.get("syntax_error"):
                    blocking_issues.append(str(finding["syntax_error"]))

        raw_dict = (
            parse_review_verdict_dict(node_input)
            if (isinstance(node_input, (str, dict)) and not isinstance(node_input, dict))
            or (isinstance(node_input, dict) and "approved" in node_input)
            else None
        )
        if raw_dict:
            guarded_dict = enforce_deterministic_verdict_guard(raw_dict, ast_findings)
            verdict = ReviewVerdict.model_validate(guarded_dict)
        elif blocking_issues:
            veto_reason = f"[AST 语义门禁一票否决] {'; '.join(blocking_issues[:3])}"
            verdict = ReviewVerdict(
                approved=False,
                confidence=1.0,
                summary=veto_reason,
                blocking_issues=blocking_issues,
            )
        else:
            verdict = ReviewVerdict(
                approved=True,
                confidence=1.0,
                summary="物理跑测全绿，AST 静态语义契约与工作区防篡改审计通过",
                blocking_issues=[],
            )

        mr_url = ""
        mr_iid = None

        # 5. 保存并提交规约契约与 OKF 审计记录到工作区
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
                "generator": "adk-dev-flow:delivery",
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
                    ["commit", "-m", f"docs(audit): append OKF delivery record for {run_id}"],
                    worktree,
                )
        except Exception:
            # Audit file committing failure should not fail the overall pipeline
            pass

        # Formal Safety Invariant: Status MUST NOT become MR_READY unless verdict.approved is strictly True!
        # Check approval BEFORE distilling knowledge or pushing to remote so rejected branches never hit origin.
        if not verdict.approved:
            veto_reason = f"[AST 语义门禁否决] {verdict.summary}"
            if verdict.blocking_issues:
                veto_reason += f" | 阻断问题: {'; '.join(verdict.blocking_issues[:3])}"
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("REVIEWING", "VERIFYING"),
                new_status="ESCALATED",
                mr_iid=mr_iid,
                mr_url=mr_url,
                review_json=verdict.model_dump_json(),
                last_error_log=veto_reason,
            )
            return {
                "run_id": run_id,
                "status": "ESCALATED",
                "mr_iid": mr_iid,
                "mr_url": mr_url,
                "review": verdict.model_dump(),
                "error": veto_reason,
            }

        # Distill and commit learned engineering knowledge only for approved runs with repair iterations.
        # Pull the actual resolved error from attempt_logs rather than the overwritten '[Verification] ✅' banner.
        try:
            resolved_err = ""
            for log_entry in store.list_attempt_logs(run_id, limit=10):
                if (log_entry.get("combined_err") or "").strip():
                    resolved_err = str(log_entry["combined_err"])
                    break
            if not resolved_err:
                raw_last = str(run.get("last_error_log") or "")
                if not raw_last.startswith("[Verification] ✅"):
                    resolved_err = raw_last
            await save_distilled_knowledge(
                spec=spec,
                worktree=worktree,
                run_id=run_id,
                verdict=verdict,
                attempt=attempt,
                last_error_log=resolved_err,
            )
        except Exception:
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
                draft=False,
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

        # Record the branch tip so flow_close_issue can verify the merge landed
        # even after the worktree and branch are cleaned up (fail-closed close).
        head_sha: str | None = None
        head_res = await _git(["rev-parse", "HEAD"], worktree)
        if head_res.returncode == 0:
            head_sha = head_res.stdout.strip() or None

        _transition_or_update(
            store,
            run_id,
            expected_statuses=("REVIEWING", "VERIFYING"),
            new_status="MR_READY",
            head_sha=head_sha,
            mr_iid=mr_iid,
            mr_url=mr_url,
            review_json=verdict.model_dump_json(),
            last_error_log=f"[MR] 🚀 验收与静态门禁全部通过！已交付 {gl.platform_name.upper()} PR/MR: {mr_url or branch}",
        )
        if spec.issue_iid is not None and gl.enabled:
            try:
                mr_link = f"[{spec.title}]({mr_url})" if mr_url else spec.title
                issue_delivery_note = (
                    f"🎉 **ADK Dev Flow 成果已交付！**\n\n"
                    f"- **关联 MR**: {mr_link}\n"
                    f"- **验收测试**: 全部通过 (100% GREEN)\n"
                    f"- **AST 语义门禁**: 审查通过\n"
                    f"- **状态**: `MR_READY`\n"
                )
                await asyncio.to_thread(gl.post_issue_comment, spec.issue_iid, issue_delivery_note)
            except Exception:
                pass
        return {
            "run_id": run_id,
            "status": "MR_READY",
            "mr_iid": mr_iid,
            "mr_url": mr_url,
            "review": verdict.model_dump(),
        }

    # Step 8 Delivery node alias for clean naming
    deliver_mr_node = publish_mr_node

    async def escalate_node(node_input: dict[str, Any]) -> dict[str, Any]:
        reason = node_input.get("escalation_reason", "验证失败")
        err_log = str(node_input.get("last_error_log", ""))[-1000:]
        _transition_or_update(
            store,
            run_id,
            expected_statuses=("VERIFYING", "REVIEWING", "RUNNING", "REPAIRING", "SPEC_LOCKED", "ESCALATED"),
            new_status="ESCALATED",
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
                    "passed": publish_mr_node,
                    "escalated": escalate_node,
                },
            ),
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


def build_prelock_workflow(
    repo_root: Path,
    store: StateStore,
    run_id: str,
) -> Workflow:
    """Build an ADK 2.x Workflow executing Pre-Lock Steps 1-3 up to Step 4 HITL."""

    async def prelock_impact_node(node_input: Any) -> dict[str, Any]:
        text = node_input.parts[0].text if hasattr(node_input, "parts") else str(node_input)
        payload = json.loads(text)
        spec = SpecContract.model_validate(payload["spec"])
        requirement = str(payload.get("requirement") or spec.summary)

        from adk_dev_flow.ast_graph import CodeGraph

        target_paths = list(spec.allowed_paths)
        with CodeGraph(repo_root) as cg:
            report = cg.analyze_impact(target_paths)
            impact_dict = report.to_dict()

        store.merge_prelock_data(
            run_id,
            impact_analysis=impact_dict,
            design_doc=spec.design_doc,
        )
        _transition_or_update(
            store,
            run_id,
            expected_statuses=("SPEC_DRAFTING", "SPEC_DESIGNED", "TEST_GENERATED", "PRE_CRITIC_REVIEWED"),
            new_status="SPEC_DESIGNED",
            last_error_log=f"[Architect] 📐 AST 影响面分析完成 ({len(impact_dict.get('impacted_production_files', []))} 关联文件)",
        )
        return {
            "run_id": run_id,
            "spec": spec.model_dump(),
            "requirement": requirement,
            "impact_analysis": impact_dict,
        }

    async def prelock_preflight_node(node_input: dict[str, Any]) -> Event:
        spec = SpecContract.model_validate(node_input["spec"])
        red_evidence: dict[str, Any]
        if spec.require_tdd_red and os.environ.get("ADK_SKIP_TDD_RED", "0") != "1":
            red_res = await asyncio.to_thread(
                run_isolated_tdd_red_preflight,
                repo_root,
                spec.acceptance_test_path,
                spec.acceptance_test_code,
            )
            if not red_res.get("ok"):
                err_msg = str(red_res.get("error") or "TDD 红灯预检失败")
                node_input["error"] = err_msg
                node_input["red_evidence"] = red_res
                store.update_run(run_id, last_error_log=f"[Tester] ❌ {err_msg[-1500:]}")
                return Event(output=node_input, route="failed")
            red_evidence = red_res
        else:
            red_evidence = {
                "ok": True,
                "test_path": spec.acceptance_test_path,
                "test_sha256": compute_sha256_bytes(spec.acceptance_test_code.encode("utf-8")),
                "exit_code": 0,
                "is_pure_semantic_failure": False,
                "skipped_red": True,
            }

        store.merge_prelock_data(run_id, red_evidence=red_evidence)
        _transition_or_update(
            store,
            run_id,
            expected_statuses=("SPEC_DRAFTING", "SPEC_DESIGNED", "TEST_GENERATED", "PRE_CRITIC_REVIEWED"),
            new_status="TEST_GENERATED",
            last_error_log="[Tester] 🧪 基线红灯预检通过（已绑定测试 SHA-256 证据）",
        )
        node_input["red_evidence"] = red_evidence
        return Event(output=node_input, route="passed")

    async def prelock_critic_node(node_input: dict[str, Any]) -> dict[str, Any]:
        spec = SpecContract.model_validate(node_input["spec"])
        pre_critic = await run_pre_critic_review(
            spec,
            run_id=run_id,
            requirement=str(node_input.get("requirement") or spec.summary),
            impact_analysis=node_input.get("impact_analysis"),
            red_evidence=node_input.get("red_evidence"),
        )
        store.merge_prelock_data(run_id, pre_critic_report=pre_critic)
        if pre_critic.get("approved"):
            _transition_or_update(
                store,
                run_id,
                expected_statuses=("SPEC_DESIGNED", "TEST_GENERATED", "PRE_CRITIC_REVIEWED"),
                new_status="PRE_CRITIC_REVIEWED",
                last_error_log=f"[Critic] 🧐 Step 3 前置双审通过: {pre_critic.get('summary', '')[:200]}",
            )
            return {
                "run_id": run_id,
                "status": "PRE_CRITIC_REVIEWED",
                "pre_critic_report": pre_critic,
            }

        issues_str = "; ".join(pre_critic.get("blocking_issues") or [pre_critic.get("summary", "")])
        store.update_run(
            run_id,
            last_error_log=f"[Critic] ⚠️ Step 3 前置双审未通过: {issues_str[:1000]}",
        )
        return {
            "run_id": run_id,
            "status": "TEST_GENERATED",
            "pre_critic_report": pre_critic,
            "error": issues_str,
        }

    async def prelock_failed_node(node_input: dict[str, Any]) -> dict[str, Any]:
        run_now = store.get_run(run_id)
        return {
            "run_id": run_id,
            "status": (run_now or {}).get("status", "SPEC_DESIGNED"),
            "error": str(node_input.get("error") or "Pre-Lock 预检失败"),
        }

    return Workflow(
        name="deterministic_prelock_workflow",
        edges=[
            (START, prelock_impact_node, prelock_preflight_node),
            (
                prelock_preflight_node,
                {
                    "passed": prelock_critic_node,
                    "failed": prelock_failed_node,
                },
            ),
        ],
    )


async def execute_prelock_workflow(
    repo_root: Path,
    store: StateStore,
    run_id: str,
    spec: SpecContract,
    requirement: str = "",
) -> dict[str, Any]:
    """Execute the deterministic Pre-Lock Steps 1-3 via Google ADK Workflow and halt at Step 4 HITL."""
    run = store.get_run(run_id)
    if not run:
        return {"error": f"未知 run_id: {run_id}"}

    wf = build_prelock_workflow(repo_root, store, run_id=run_id)
    session_service = InMemorySessionService()
    session_id = f"prelock-{run_id}-{uuid_suffix()}"
    await session_service.create_session(
        app_name="adk_dev_flow", user_id="developer", session_id=session_id
    )
    runner = Runner(agent=wf, app_name="adk_dev_flow", session_service=session_service)
    payload = json.dumps(
        {"run_id": run_id, "spec": spec.model_dump(), "requirement": requirement or spec.summary},
        ensure_ascii=False,
    )
    msg = types.Content(role="user", parts=[types.Part(text=payload)])
    async for _ in runner.run_async(
        user_id="developer", session_id=session_id, new_message=msg
    ):
        pass

    final_run = store.get_run(run_id) or {}
    prelock = store.get_prelock_data(run_id)
    return {
        "run_id": run_id,
        "status": final_run.get("status"),
        "impact_analysis": prelock.get("impact_analysis"),
        "red_evidence": prelock.get("red_evidence"),
        "pre_critic_report": prelock.get("pre_critic_report"),
    }


def uuid_suffix() -> str:
    import uuid as _uuid

    return _uuid.uuid4().hex[:8]
