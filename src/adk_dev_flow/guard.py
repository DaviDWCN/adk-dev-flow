from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Sequence

PROTECTED_PATHS: tuple[str, ...] = (
    ".gitlab-ci.yml",
    ".codebuddy",
    ".adk/state.db",
    ".adk/active_run.json",
    ".adk/worktrees",
    ".adk/locks",
    "mcp.json",
    "tools/adk_dev_flow",
    ".git",
    ".gitignore",
    ".gitattributes",
    "sitecustomize.py",
    "usercustomize.py",
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
)

OKF_WRITABLE_PATHS: tuple[str, ...] = (
    ".adk/specs",
    ".adk/knowledge",
    ".adk/audit",
)


def normalize_relative_path(raw_path: str, base_dir: Path) -> str | None:
    """Resolve raw_path against base_dir and return a clean POSIX relative path.

    Returns None if the resolved path escapes base_dir (path traversal or outside workspace).
    """
    if not raw_path or not raw_path.strip():
        return None
    base = base_dir.resolve()
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        rel = resolved.relative_to(base).as_posix()
    except ValueError:
        return None
    if rel == ".":
        return ""
    return rel


def strip_dot_slash(path: str) -> str:
    """Strip leading './' prefixes and slashes without stripping leading dots of dotfiles."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def is_path_under(rel_path: str, prefix: str) -> bool:
    """Directory-boundary-safe prefix check (prevents 'src/api' matching 'src/api_admin/x.py')."""
    norm_rel = strip_dot_slash(rel_path).rstrip("/")
    norm_prefix = strip_dot_slash(prefix).rstrip("/")
    if not norm_prefix:
        return False
    return norm_rel == norm_prefix or norm_rel.startswith(f"{norm_prefix}/")


def is_protected_path(rel_path: str, protected: Sequence[str] = PROTECTED_PATHS) -> bool:
    if any(is_path_under(rel_path, p) for p in OKF_WRITABLE_PATHS):
        return False
    return any(is_path_under(rel_path, p) for p in protected)


def evaluate_file_edit(
    file_path: str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Evaluate whether a file edit is permitted under ADK workflow guardrails.

    Returns (allowed, reason).
    """
    env_map = env if env is not None else dict(os.environ)
    rel = normalize_relative_path(file_path, cwd)
    if rel is None:
        return False, f"路径 `{file_path}` 超出当前工作目录 `{cwd.resolve()}`，禁止越界写入"
    if not rel:
        return False, "禁止直接写入工作目录根路径"

    worker_token = env_map.get("ADK_WORKER_TOKEN", "").strip()
    if worker_token:
        if is_protected_path(rel):
            return False, f"禁止修改受保护的门禁或框架文件: {rel}"
        try:
            locked_raw = json.loads(env_map.get("ADK_LOCKED_TESTS", "[]"))
            allowed_raw = json.loads(env_map.get("ADK_ALLOWED_PATHS", "[]"))
        except json.JSONDecodeError as exc:
            return False, f"Worker 白名单/锁定测试环境变量 JSON 解析失败，安全拦截: {exc}"

        if not isinstance(locked_raw, list) or not isinstance(allowed_raw, list):
            return False, "Worker 白名单/锁定测试环境变量格式非法，安全拦截"

        for locked_test in locked_raw:
            norm_locked = normalize_relative_path(str(locked_test), cwd)
            if norm_locked is not None and rel == norm_locked:
                return False, f"禁止篡改 Stage 1 锁定的验收测试文件: {rel}"

        if not allowed_raw:
            return False, "ADK_ALLOWED_PATHS 白名单为空，禁止写入任何业务文件"

        if not any(is_path_under(rel, str(a)) for a in allowed_raw):
            return False, f"路径 `{rel}` 超出当前 Spec 白名单 {allowed_raw}"
        return True, ""

    marker = cwd / ".adk" / "active_run.json"
    if marker.exists():
        marker_status = ""
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            marker_status = str(marker_data.get("status", "")).strip().upper()
        except Exception:
            marker_status = ""

        if marker_status == "SPEC_DRAFTING":
            if is_protected_path(rel):
                return False, f"禁止修改受保护的门禁或框架文件: {rel}"
            if any(is_path_under(rel, p) for p in OKF_WRITABLE_PATHS):
                return True, ""
            return (
                False,
                "当前处于 SPEC_DRAFTING 阶段，仅允许起草或修改 .adk/specs/ 与 .adk/knowledge/ 规约文件；"
                "严禁在主工作区修改业务代码，请在 Spec 确认后通过 flow_dispatch 调度至沙箱 Worktree 执行。",
            )

        if is_protected_path(rel):
            return False, f"禁止修改受保护的门禁或框架文件: {rel}"
        return (
            False,
            "当前处于 ADK 受控研发流中，严禁在主工作区直接修改代码；"
            "请通过 adk-dev-flow MCP 推进或调用 flow_abort / flow_unlock 退出。",
        )

    # Outside active flow: developers are free to edit project files (pyproject.toml, etc.)
    # Protect only raw internal database / runtime state if present
    if any(is_path_under(rel, p) for p in (".adk/state.db", ".adk/active_run.json", ".adk/worktrees", ".git")):
        return False, f"禁止直接修改系统内部运行状态文件: {rel}"

    return True, ""


def _block(reason: str) -> None:
    print(f"[ADK Workflow Guard 拦截] {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        _block(f"Hook 输入 JSON 无法解析，执行 Fail-Closed 拦截: {exc}")
        return

    if not isinstance(event, dict):
        _block("Hook 输入格式非法（非 JSON 对象），执行 Fail-Closed 拦截")
        return

    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        sys.exit(0)

    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if not file_path:
        sys.exit(0)

    allowed, reason = evaluate_file_edit(file_path, cwd=Path.cwd())
    if not allowed:
        _block(reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
