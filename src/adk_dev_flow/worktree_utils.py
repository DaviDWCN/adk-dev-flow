from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from adk_dev_flow.guard import is_path_under, is_protected_path, strip_dot_slash
from adk_dev_flow.subproc import run_subprocess_safe

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DURATION_RE = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_TIMING_PAREN_RE = re.compile(r"\(\d+:\d+:\d+\)")
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{6,16}")
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)
_LINE_NUM_RE = re.compile(r"(?::\d+)+")


def compute_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_file_sha256(path: Path) -> str:
    return compute_sha256_bytes(path.read_bytes())


def normalize_error_for_signature(err_text: str, worktree: Path | None = None) -> str:
    """Normalize volatile tokens (timings, ANSI, hex addresses, worktree path, line numbers) before hashing."""
    text = _ANSI_RE.sub("", err_text)
    if worktree is not None:
        wt_str = str(worktree.resolve())
        text = text.replace(wt_str, "<WORKTREE>")
    text = _ISO_TS_RE.sub("<TS>", text)
    text = _DURATION_RE.sub("in <DURATION>", text)
    text = _TIMING_PAREN_RE.sub("(<TIME>)", text)
    text = _HEX_ADDR_RE.sub("0x<ADDR>", text)
    text = _LINE_NUM_RE.sub(":<LINE>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def compute_error_signature(err_text: str, worktree: Path | None = None) -> str:
    normalized = normalize_error_for_signature(err_text, worktree=worktree)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def parse_review_verdict_dict(raw_output: Any) -> dict[str, Any]:
    """Fail-Closed parser for Critic ReviewVerdict outputs.

    Accepts dict, BaseModel-like objects with model_dump(), JSON strings (with optional
    markdown fences), or ADK Content-like objects exposing `.parts[0].text`.
    On any ambiguity or parse failure, returns `approved=False` (Fail-Closed).
    """
    payload: Any = raw_output

    # Unwrap Pydantic / dataclass-like model instance
    if hasattr(payload, "model_dump") and callable(payload.model_dump):
        try:
            payload = payload.model_dump()
        except TypeError:
            pass

    # Unwrap ADK Content / Event-like object
    if not isinstance(payload, (dict, str)):
        if hasattr(payload, "parts") and payload.parts:
            texts = [getattr(p, "text", "") for p in payload.parts if getattr(p, "text", None)]
            payload = "\n".join(texts)
        elif hasattr(payload, "content") and hasattr(payload.content, "parts"):
            texts = [
                getattr(p, "text", "")
                for p in (payload.content.parts or [])
                if getattr(p, "text", None)
            ]
            payload = "\n".join(texts)

    if isinstance(payload, str):
        cleaned = strip_markdown_code_fence(payload)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            # Attempt extracting outermost JSON object if wrapped with prose
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end > start:
                try:
                    payload = json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    return {
                        "approved": False,
                        "blocking_issues": [
                            "Critic 审查输出无法解析为有效 JSON（Fail-Closed，已保持 Draft）"
                        ],
                        "summary": cleaned[:500] or "Critic 返回空响应",
                    }
            else:
                return {
                    "approved": False,
                    "blocking_issues": [
                        "Critic 审查输出非结构化 JSON（Fail-Closed，已保持 Draft）"
                    ],
                    "summary": cleaned[:500] or "Critic 返回空响应",
                }

    if not isinstance(payload, dict):
        return {
            "approved": False,
            "blocking_issues": [
                f"Critic 审查输出类型异常 ({type(raw_output).__name__})，执行 Fail-Closed 拦截"
            ],
            "summary": str(raw_output)[:500],
        }

    if "approved" not in payload or not isinstance(payload["approved"], bool):
        return {
            "approved": False,
            "blocking_issues": ["Critic 审查 JSON 缺少布尔字段 `approved`（Fail-Closed）"],
            "summary": str(payload.get("summary") or json.dumps(payload, ensure_ascii=False))[:500],
        }

    blocking_raw = payload.get("blocking_issues")
    if blocking_raw is None:
        blocking_issues: list[str] = []
    elif isinstance(blocking_raw, list):
        blocking_issues = [str(item) for item in blocking_raw]
    else:
        blocking_issues = [str(blocking_raw)]

    summary = str(payload.get("summary") or "").strip() or (
        "Critic 审查通过" if payload["approved"] else "Critic 审查未通过"
    )
    return {
        "approved": payload["approved"],
        "blocking_issues": blocking_issues,
        "summary": summary,
    }


def _find_venv_bin(base_dir: Path, tool_name: str) -> Path | None:
    candidates = [
        base_dir / ".venv" / "bin" / tool_name,
        base_dir / ".venv" / "Scripts" / f"{tool_name}.exe",
        base_dir / "venv" / "bin" / tool_name,
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_tool_command(
    tool_name: str,
    default_args: Sequence[str],
    *,
    worktree: Path,
    repo_root: Path,
    env_override: str | None = None,
    placeholder_vars: dict[str, str] | None = None,
) -> list[str] | None:
    """Resolve lint/test command so it executes inside the target repo's environment.

    Resolution priority:
    1. Explicit env_override (returns None if set to 'skip' or 'none')
    2. `{worktree}/.venv/bin/{tool}`
    3. `{repo_root}/.venv/bin/{tool}` (critical since git worktrees don't copy gitignored .venv)
    4. `uv run --project {repo_root} {tool}` if `uv` and `{repo_root}/pyproject.toml` exist
    5. System PATH `{tool}`
    """
    if env_override is not None:
        trimmed = env_override.strip()
        if trimmed.lower() in ("skip", "none", "off"):
            return None
        if trimmed:
            rendered = trimmed.format(**(placeholder_vars or {}))
            return shlex.split(rendered)

    for candidate_root in (worktree, repo_root):
        found = _find_venv_bin(candidate_root, tool_name)
        if found is not None:
            return [str(found), *default_args]

    uv_bin = shutil.which("uv")
    if uv_bin and (repo_root / "pyproject.toml").is_file():
        return [uv_bin, "run", "--project", str(repo_root), tool_name, *default_args]

    sys_bin = shutil.which(tool_name)
    if sys_bin:
        return [sys_bin, *default_args]

    return [tool_name, *default_args]


@dataclass
class WorktreeAuditResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    locked_test_tampered: bool = False


def _parse_porcelain_paths(stdout: str) -> list[str]:
    paths: list[str] = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            old_p, new_p = raw.split(" -> ", 1)
            paths.append(old_p.strip('"'))
            paths.append(new_p.strip('"'))
        else:
            paths.append(raw.strip('"'))
    return paths


async def sanitize_worktree_state(worktree: Path) -> None:
    """Clean up stale git index locks and unsanitized artifacts from interrupted runs."""
    if not worktree.exists():
        return
    index_lock = worktree / ".git" / "index.lock"
    if index_lock.is_file():
        try:
            index_lock.unlink()
        except OSError:
            pass

    git_file = worktree / ".git"
    if git_file.is_file():
        try:
            content = git_file.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                actual_dir = Path(content.split("gitdir:", 1)[1].strip())
                wt_lock = actual_dir / "index.lock"
                if wt_lock.is_file():
                    wt_lock.unlink()
        except Exception:
            pass


async def audit_worktree_changes(
    worktree: Path,
    *,
    allowed_paths: Sequence[str],
    acceptance_test_path: str,
    locked_test_sha256: str,
    locked_test_commit_sha: str,
) -> WorktreeAuditResult:
    """Unbypassable post-worker Git & hash audit inside the isolated worktree.

    Catches any edits made via Bash/Python scripts that bypassed PreToolUse hooks:
    1. Verifies acceptance_test_path exists and matches its locked SHA-256.
    2. Collects all uncommitted, untracked, and worker-committed files since locked_test_commit_sha.
    3. Rejects any touched protected files or files outside `allowed_paths`.
    """
    violations: list[str] = []
    locked_test_tampered = False

    norm_test = strip_dot_slash(acceptance_test_path)
    test_file = worktree / norm_test

    if not test_file.is_file():
        locked_test_tampered = True
        violations.append(f"锁定的验收测试文件被删除或移动: {norm_test}")
    else:
        actual_sha = compute_file_sha256(test_file)
        if actual_sha != locked_test_sha256:
            locked_test_tampered = True
            violations.append(
                f"锁定的验收测试文件 `{norm_test}` 内容被篡改 "
                f"(expected={locked_test_sha256[:12]}, actual={actual_sha[:12]})"
            )

    if locked_test_tampered and locked_test_commit_sha:
        # Restore the tampered test file from the immutable lock commit.
        await run_subprocess_safe(
            ["git", "checkout", locked_test_commit_sha, "--", norm_test],
            cwd=worktree,
            timeout_sec=30.0,
        )

    # Check for .git/info/exclude tampering
    git_dir = worktree / ".git"
    exclude_file = None
    if git_dir.is_dir():
        exclude_file = git_dir / "info" / "exclude"
    elif git_dir.is_file():
        try:
            content = git_dir.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                actual_dir = Path(content.split("gitdir:", 1)[1].strip())
                exclude_file = actual_dir / "info" / "exclude"
        except Exception:
            pass

    if exclude_file and exclude_file.is_file():
        try:
            lines = [l.strip() for l in exclude_file.read_text(encoding="utf-8", errors="replace").splitlines()]
            non_comments = [l for l in lines if l and not l.startswith("#")]
            if non_comments:
                violations.append("检测到受保护配置篡改: 禁止修改或利用 .git/info/exclude 掩盖文件变更")
        except Exception:
            pass

    # Check for git index evasion flags (assume-unchanged, skip-worktree)
    ls_files_res = await run_subprocess_safe(
        ["git", "ls-files", "-v"],
        cwd=worktree,
        timeout_sec=30.0,
    )
    if ls_files_res.returncode == 0:
        for line in ls_files_res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            flag = line_str[0]
            if flag in ("h", "s", "m"):
                f_name = line_str[2:].strip()
                violations.append(f"检测到非法 Git 索引伪装标记 ({flag}): `{f_name}`")

    status_res = await run_subprocess_safe(
        ["git", "status", "--porcelain", "-uall", "--ignored=matching"],
        cwd=worktree,
        timeout_sec=30.0,
    )
    if status_res.returncode != 0:
        violations.append(f"git status 审计失败: {status_res.stderr.strip()}")
        return WorktreeAuditResult(
            ok=False,
            violations=violations,
            locked_test_tampered=locked_test_tampered,
        )

    touched_set: set[str] = set()
    for line in status_res.stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            old_p, new_p = raw.split(" -> ", 1)
            raw_candidates = [old_p.strip('"'), new_p.strip('"')]
        else:
            raw_candidates = [raw.strip('"')]
        for rc in raw_candidates:
            norm_rc = strip_dot_slash(rc)
            if norm_rc.startswith((".venv/", "venv/", "__pycache__/", ".pytest_cache/", ".adk/")):
                continue
            touched_set.add(norm_rc)

    if locked_test_commit_sha:
        diff_commits_res = await run_subprocess_safe(
            ["git", "diff", "--name-only", f"{locked_test_commit_sha}..HEAD"],
            cwd=worktree,
            timeout_sec=30.0,
        )
        if diff_commits_res.returncode == 0:
            for line in diff_commits_res.stdout.splitlines():
                p = line.strip()
                if p:
                    touched_set.add(p)

    touched_files = sorted(touched_set)
    for rel in touched_files:
        norm_rel = strip_dot_slash(rel)
        if is_protected_path(norm_rel):
            violations.append(f"工兵越权修改了受保护的门禁/框架文件: {norm_rel}")
            continue
        if norm_rel == norm_test:
            # Already checked above via SHA-256
            continue
        if not any(is_path_under(norm_rel, allowed) for allowed in allowed_paths):
            violations.append(f"工兵越权修改了白名单外文件: `{norm_rel}` (允许: {list(allowed_paths)})")

    return WorktreeAuditResult(
        ok=len(violations) == 0,
        violations=violations,
        touched_files=touched_files,
        locked_test_tampered=locked_test_tampered,
    )
