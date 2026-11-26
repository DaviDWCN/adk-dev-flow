#!/usr/bin/env python3
"""ADK flow commit gate — PreToolUse/Bash hook (standalone, no package imports).

Deliberately duplicated inlined logic from adk_dev_flow.enforcement: the
plugin cache must stay portable, it cannot import the package on every
machine. If the package IS importable, prefer it (single source of truth):
    try: from adk_dev_flow.enforcement import ...
    except ImportError: <inline fallback>

Policy (L1'): only gate Bash commands that run ``git commit``. Editing and
exploration are never gated. A branch declaring an issue (``NNN-*`` /
``adk/issue-N-*``) requires that issue to have a non-ABORTED run; the deny
reason points to flow_adopt — the cheap compliance path must exist before
the gate does (lesson: run 558a138f54eb).
Exemption: ADK_FLOW_ENFORCE=off allows the commit but appends a trace to
.adk/exemptions.log — a silent exemption is a silent evidence-chain break.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ISSUE_BRANCH_PATTERNS = (re.compile(r"^(\d+)-"), re.compile(r"^adk/issue-(\d+)"))
COMMIT_RE = re.compile(r"\bgit\b(?:\s+-{1,2}[\w-]+(?:\s+\S+)?)*\s+commit(?![\w-])|\bgit\b\s+-C\s+\S+\s+commit(?![\w-])")

# init-and-forget defense: SPEC_DRAFTING drafts are transient; older than
# this they no longer legitimize commits (mirrors enforcement.py).
SPEC_DRAFTING_STALE_HOURS = 24


def _has_fresh_registration(rows) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SPEC_DRAFTING_STALE_HOURS)
    for status, updated_at in rows:
        if status != "SPEC_DRAFTING":
            return True
        try:
            ts = datetime.fromisoformat(str(updated_at))
        except (TypeError, ValueError):
            return True  # unparseable timestamp: treat conservatively as registered
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            return True
    return False


def find_flow_root(start: Path) -> Path | None:
    start = Path(start).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".adk" / "workflow.db").exists():
            return candidate
    return None


def extract_issue(branch: str) -> int | None:
    for pattern in ISSUE_BRANCH_PATTERNS:
        m = pattern.match((branch or "").strip())
        if m:
            try:
                return int(m.group(1))
            except (IndexError, ValueError):
                return None
    return None


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unreadable input: allow, do not brick the session
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    if not cmd or not COMMIT_RE.search(cmd):
        return 0  # fast path: not a git commit

    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    flow_root = find_flow_root(cwd)
    if flow_root is None:
        return 0  # project not enrolled in the deterministic flow

    if str(os.environ.get("ADK_FLOW_ENFORCE", "")).strip().lower() == "off":
        try:
            log = flow_root / ".adk" / "exemptions.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"EXEMPT\tsession={payload.get('session_id', '')}\tcmd={cmd[:200]}\n")
        except Exception:
            pass
        return 0  # exempt, but traced

    try:
        # Standalone hook: no package imports — inline the Windows no-window
        # flag (CREATE_NO_WINDOW = 0x08000000) instead of subproc helpers.
        extra_kwargs = (
            {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        )
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            **extra_kwargs,
        )
        branch = res.stdout.strip()
    except Exception:
        return 0  # cannot read branch: allow, do not guess
    if not branch or branch == "HEAD":
        return 0

    issue = extract_issue(branch)
    if issue is None:
        return 0  # non-issue branch: out of scope by policy

    db_path = flow_root / ".adk" / "workflow.db"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute(
                "SELECT status, updated_at FROM runs WHERE issue_iid = ? AND status != 'ABORTED'",
                (issue,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0  # transient DB failure: fail-open, L3 keeps bypass visible

    if _has_fresh_registration(rows):
        return 0  # issue properly registered

    deny(
        f"确定性研发约束：分支 `{branch}` 宣告了 issue #{issue}，但流程库中该 issue 没有有效 run"
        f"（ABORTED 不计；SPEC_DRAFTING 超过 {SPEC_DRAFTING_STALE_HOURS} 小时视同失效，init-and-forget 不构成豁免）。"
        f"便宜合规路径：调用 flow_adopt(issue={issue}, branch='{branch}') 收编现有工作，"
        f"或走完整 flow_init 建单。此检查只在 commit 时发生，不拦编辑与探索。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
