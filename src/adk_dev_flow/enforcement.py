"""Commit-gate enforcement: issue branches must have a registered run.

Shared core for both chokepoints:
- L1' PreToolUse hook (session-level teaching moment) — ships as a
  standalone script in the plugin (no package import, plugin cache must be
  portable), see plugin/adk-dev-flow/hooks/enforce_flow.py
- L2 pre-commit hook (git-side backstop) — calls into this module via
  ``python -m adk_dev_flow.enforcement``

Policy: only commits on branches that DECLARE an issue (``NNN-*`` or
``adk/issue-N-*``) are gated; the issue must have a run whose status is not
ABORTED (an aborted run does not legitimize new commits). Branches without
an issue reference are out of scope — the constraint targets issue-driven
work, and gating chores would be a smaller-scale ritual trap.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

# Branch declares an issue: "76-sqlite-only-..." or "adk/issue-66-5ff00b"
ISSUE_BRANCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(\d+)-"),
    re.compile(r"^adk/issue-(\d+)"),
)

ADVICE = (
    "便宜合规路径：调用 flow_adopt(issue={issue}, branch='{branch}') 收编现有工作，"
    "或走完整 flow_init 建单。此检查只在 commit 时发生，不拦编辑与探索。"
)


def extract_issue(branch: str) -> int | None:
    """Return the issue number a branch declares, or None when it declares none."""
    branch = (branch or "").strip()
    for pattern in ISSUE_BRANCH_PATTERNS:
        match = pattern.match(branch)
        if match:
            try:
                return int(match.group(1))
            except (IndexError, ValueError):
                return None
    return None


def find_flow_root(start: Path) -> Path | None:
    """Walk up from start to the nearest directory holding .adk/workflow.db."""
    start = Path(start).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".adk" / "workflow.db").exists():
            return candidate
    return None


def check_branch_binding(flow_root: Path, branch: str) -> tuple[bool, str]:
    """Check a branch's declared issue has a registered, non-aborted run.

    Returns (allowed, reason). reason is empty when allowed.
    """
    issue = extract_issue(branch)
    if issue is None:
        return True, ""  # non-issue branch: out of scope
    db_path = Path(flow_root) / ".adk" / "workflow.db"
    if not db_path.exists():
        return True, ""  # project not enrolled in the flow
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE issue_iid = ? AND status != 'ABORTED' LIMIT 1",
                (issue,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return True, ""  # transient DB failure: fail-open, L3 keeps bypass visible
    if row:
        return True, ""
    reason = (
        f"确定性研发约束：分支 `{branch}` 宣告了 issue #{issue}，"
        f"但流程库中该 issue 没有有效 run（ABORTED 不计）。" + ADVICE.format(issue=issue, branch=branch)
    )
    return False, reason


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the L2 pre-commit hook: python -m adk_dev_flow.enforcement."""
    import subprocess

    cwd = Path.cwd()
    flow_root = find_flow_root(cwd)
    if flow_root is None:
        return 0
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = res.stdout.strip()
    except Exception:
        return 0
    if not branch or branch == "HEAD":
        return 0
    allowed, reason = check_branch_binding(flow_root, branch)
    if allowed:
        return 0
    print(reason, file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
