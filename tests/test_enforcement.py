from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.enforcement import (
    check_branch_binding,
    extract_issue,
    find_flow_root,
    install_pre_commit_hook,
)

HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "plugin" / "adk-dev-flow" / "hooks" / "enforce_flow.py"


class TestIssueExtraction(unittest.TestCase):
    def test_number_prefix(self) -> None:
        self.assertEqual(extract_issue("76-sqlite-only-canonical-database"), 76)
        self.assertEqual(extract_issue("67-a2-auth-wheelhouse"), 67)

    def test_adk_issue_pattern(self) -> None:
        self.assertEqual(extract_issue("adk/issue-66-5ff00b"), 66)

    def test_non_issue_branches(self) -> None:
        self.assertIsNone(extract_issue("main"))
        self.assertIsNone(extract_issue("feature/misc"))
        self.assertIsNone(extract_issue(""))


class TestFlowRootDiscovery(unittest.TestCase):
    def test_walks_up_to_flow_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".adk").mkdir()
            (root / ".adk" / "workflow.db").write_bytes(b"")
            deep = root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            self.assertEqual(find_flow_root(deep), root)

    def test_none_when_not_enrolled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_flow_root(Path(tmp)))


class TestBranchBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".adk").mkdir()
        self.db = self.root / ".adk" / "workflow.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, issue_iid INTEGER, status TEXT NOT NULL,"
            " updated_at TEXT)"
        )
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add_run(self, run_id: str, issue: int, status: str, updated_at: str | None = None) -> None:
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "INSERT INTO runs (run_id, issue_iid, status, updated_at) VALUES (?, ?, ?, ?)",
            (run_id, issue, status, updated_at),
        )
        conn.commit()
        conn.close()

    def test_zombie_spec_drafting_denied(self) -> None:
        from datetime import datetime, timedelta, timezone

        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self._add_run("r1", 76, "SPEC_DRAFTING", updated_at=stale)
        allowed, reason = check_branch_binding(self.root, "76-doc-adr")
        self.assertFalse(allowed)
        self.assertIn("init-and-forget", reason)

    def test_fresh_spec_drafting_allowed(self) -> None:
        from datetime import datetime, timedelta, timezone

        fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self._add_run("r1", 76, "SPEC_DRAFTING", updated_at=fresh)
        allowed, _ = check_branch_binding(self.root, "76-doc-adr")
        self.assertTrue(allowed)

    def test_old_adopted_still_counts(self) -> None:
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self._add_run("r1", 76, "ADOPTED", updated_at=old)
        allowed, _ = check_branch_binding(self.root, "76-doc-adr")
        self.assertTrue(allowed)

    def test_allow_when_issue_registered_active(self) -> None:
        self._add_run("r1", 76, "ADOPTED")
        allowed, _ = check_branch_binding(self.root, "76-doc-adr")
        self.assertTrue(allowed)

    def test_allow_when_issue_registered_terminal(self) -> None:
        self._add_run("r1", 76, "MR_READY")
        allowed, _ = check_branch_binding(self.root, "76-doc-adr")
        self.assertTrue(allowed)

    def test_deny_when_no_run(self) -> None:
        allowed, reason = check_branch_binding(self.root, "76-doc-adr")
        self.assertFalse(allowed)
        self.assertIn("flow_adopt(issue=76", reason)

    def test_deny_when_only_aborted_run(self) -> None:
        self._add_run("r1", 76, "ABORTED")
        allowed, reason = check_branch_binding(self.root, "76-doc-adr")
        self.assertFalse(allowed)
        self.assertIn("ABORTED 不计", reason)

    def test_allow_non_issue_branch(self) -> None:
        allowed, reason = check_branch_binding(self.root, "chore/misc")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")


def _run_hook(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    )


class TestPreCommitInstall(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, capture_output=True)
        self.db = self.repo / ".adk" / "workflow.db"
        (self.repo / ".adk").mkdir()
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, issue_iid INTEGER, status TEXT NOT NULL,"
            " updated_at TEXT)"
        )
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _hook_path(self) -> Path:
        res = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"], cwd=self.repo, capture_output=True, text=True
        )
        hooks = Path(res.stdout.strip())
        if not hooks.is_absolute():
            hooks = self.repo / hooks
        return hooks / "pre-commit"

    def test_install_creates_hook(self) -> None:
        res = install_pre_commit_hook(self.repo)
        self.assertTrue(res["installed"])
        content = self._hook_path().read_text(encoding="utf-8")
        self.assertIn("adk_dev_flow.enforcement", content)

    def test_install_idempotent(self) -> None:
        install_pre_commit_hook(self.repo)
        again = install_pre_commit_hook(self.repo)
        self.assertFalse(again["installed"])
        self.assertEqual(again["reason"], "already installed")

    def test_existing_foreign_hook_preserved(self) -> None:
        hook = self._hook_path()
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        res = install_pre_commit_hook(self.repo)
        self.assertFalse(res["installed"])
        self.assertEqual(res["reason"], "existing pre-commit hook preserved")
        self.assertIn("custom", hook.read_text(encoding="utf-8"))

    def test_end_to_end_commit_gate(self) -> None:
        """Real pre-commit run: unregistered issue branch blocks, registered passes."""
        install_pre_commit_hook(self.repo)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", "-b", "77-gate-test"], cwd=self.repo, check=True, capture_output=True
        )
        blocked = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "wip"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        pass

        self.assertNotEqual(blocked.returncode, 0, "unregistered issue branch must be blocked")
        self.assertIn("flow_adopt", blocked.stderr, "deny reason must reach the committer (L2 teaching moment)")
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO runs (run_id, issue_iid, status) VALUES ('r1', 77, 'ADOPTED')")
        conn.commit()
        conn.close()
        passed = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "wip2"],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(passed.returncode, 0, "registered branch must commit cleanly")


class TestHookScript(unittest.IsolatedAsyncioTestCase, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        (self.repo / ".adk").mkdir()
        self.db = self.repo / ".adk" / "workflow.db"
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, issue_iid INTEGER, status TEXT NOT NULL,"
            " updated_at TEXT)"
        )
        conn.close()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "76-doc-adr"], cwd=self.repo, check=True, capture_output=True
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add_run(self, run_id: str, issue: int, status: str, updated_at: str | None = None) -> None:
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "INSERT INTO runs (run_id, issue_iid, status, updated_at) VALUES (?, ?, ?, ?)",
            (run_id, issue, status, updated_at),
        )
        conn.commit()
        conn.close()

    def _payload(self, command: str) -> dict:
        return {
            "session_id": "s1",
            "cwd": str(self.repo),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def test_deny_commit_on_unregistered_issue_branch(self) -> None:
        res = _run_hook(self._payload("git commit -m 'wip'"), self.repo)
        decision = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("flow_adopt(issue=76", decision["permissionDecisionReason"])

    def test_deny_zombie_spec_drafting_commit(self) -> None:
        from datetime import datetime, timedelta, timezone

        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self._add_run("r1", 76, "SPEC_DRAFTING", updated_at=stale)
        res = _run_hook(self._payload("git commit -m 'wip'"), self.repo)
        decision = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("init-and-forget", decision["permissionDecisionReason"])

    def test_allow_commit_when_registered(self) -> None:
        self._add_run("r1", 76, "ADOPTED")
        res = _run_hook(self._payload("git commit -m 'wip'"), self.repo)
        self.assertEqual(res.stdout.strip(), "")

    def test_allow_non_commit_commands(self) -> None:
        for cmd in ("git status", "git log -1", "ls -la", "git commit-tree HEAD", "echo hi"):
            res = _run_hook(self._payload(cmd), self.repo)
            self.assertEqual(res.stdout.strip(), "", cmd)

    def test_allow_non_issue_branch_commit(self) -> None:
        subprocess.run(["git", "checkout", "-q", "-b", "chore/misc"], cwd=self.repo, check=True, capture_output=True)
        res = _run_hook(self._payload("git commit -m 'x'"), self.repo)
        self.assertEqual(res.stdout.strip(), "")

    def test_exempt_leaves_trace(self) -> None:
        env_cmd = _run_hook(self._payload("git commit -m 'wip'"), self.repo)
        # simulate exemption via env: rerun with ADK_FLOW_ENFORCE=off
        import os

        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps(self._payload("git commit -m 'wip'")),
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            timeout=30,
            env={**os.environ, "ADK_FLOW_ENFORCE": "off"},
        )
        self.assertEqual(proc.stdout.strip(), "")
        self.assertTrue(env_cmd.returncode == 0)
        trace = (self.repo / ".adk" / "exemptions.log").read_text(encoding="utf-8")
        self.assertIn("EXEMPT", trace)
        self.assertIn("git commit", trace)

    def test_allow_when_project_not_enrolled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp)
            payload = {
                "session_id": "s1",
                "cwd": str(plain),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git commit -m 'x'"},
            }
            res = _run_hook(payload, plain)
            self.assertEqual(res.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
