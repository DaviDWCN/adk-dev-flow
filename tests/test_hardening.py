from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.guard import evaluate_file_edit
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.providers import MockWorkerProvider, get_worker_provider
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.worktree_utils import (
    audit_worktree_changes,
    compute_error_signature,
    compute_file_sha256,
    sanitize_worktree_state,
    sync_worktree_dependencies,
)


class TestHardeningSuite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self._tmp.name).resolve()
        await run_subprocess_safe(["git", "init"], cwd=self.wt)
        await run_subprocess_safe(["git", "config", "user.name", "Tester"], cwd=self.wt)
        await run_subprocess_safe(["git", "config", "user.email", "tester@test.com"], cwd=self.wt)

        self.test_path = "tests/test_target.py"
        (self.wt / "tests").mkdir(parents=True)
        t_file = self.wt / self.test_path
        t_file.write_text("def test_ok(): assert True\n", encoding="utf-8")
        self.locked_sha = compute_file_sha256(t_file)

        await run_subprocess_safe(["git", "add", "."], cwd=self.wt)
        await run_subprocess_safe(["git", "commit", "-m", "init"], cwd=self.wt)
        res = await run_subprocess_safe(["git", "rev-parse", "HEAD"], cwd=self.wt)
        self.commit_sha = res.stdout.strip()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_detects_sitecustomize_and_gitignore_bypass(self) -> None:
        # Worker attempts to bypass by injecting sitecustomize.py and ignoring it
        (self.wt / "sitecustomize.py").write_text("import pytest\n# mock tests\n", encoding="utf-8")
        (self.wt / ".gitignore").write_text("sitecustomize.py\n", encoding="utf-8")

        audit = await audit_worktree_changes(
            self.wt,
            allowed_paths=["src/"],
            acceptance_test_path=self.test_path,
            locked_test_sha256=self.locked_sha,
            locked_test_commit_sha=self.commit_sha,
        )
        self.assertFalse(audit.ok)
        violations_str = " ".join(audit.violations)
        self.assertIn("sitecustomize.py", violations_str)
        self.assertIn(".gitignore", violations_str)

    async def test_detects_assume_unchanged_index_tampering(self) -> None:
        # Create a dummy protected file and commit it
        ci_file = self.wt / ".gitlab-ci.yml"
        ci_file.write_text("stages: [test]\n", encoding="utf-8")
        await run_subprocess_safe(["git", "add", ".gitlab-ci.yml"], cwd=self.wt)
        await run_subprocess_safe(["git", "commit", "-m", "add ci"], cwd=self.wt)

        # Worker sets assume-unchanged and modifies it
        await run_subprocess_safe(
            ["git", "update-index", "--assume-unchanged", ".gitlab-ci.yml"], cwd=self.wt
        )
        ci_file.write_text("stages: []\n", encoding="utf-8")

        audit = await audit_worktree_changes(
            self.wt,
            allowed_paths=["src/"],
            acceptance_test_path=self.test_path,
            locked_test_sha256=self.locked_sha,
            locked_test_commit_sha=self.commit_sha,
        )
        self.assertFalse(audit.ok)
        self.assertTrue(any("索引伪装标记" in v for v in audit.violations))

    async def test_sanitize_worktree_removes_stale_index_lock(self) -> None:
        lock_file = self.wt / ".git" / "index.lock"
        lock_file.write_text("locked", encoding="utf-8")
        self.assertTrue(lock_file.is_file())

        await sanitize_worktree_state(self.wt)
        self.assertFalse(lock_file.is_file())

    def test_semantic_error_signature_ignores_line_drift(self) -> None:
        log1 = (
            "FAILED tests/test_core.py:42: AssertionError: assert 0 == 1\n"
            "src/core.py:105: in execute\n"
            "    raise ValueError('invalid')"
        )
        log2 = (
            "FAILED tests/test_core.py:58: AssertionError: assert 0 == 1\n"
            "src/core.py:121: in execute\n"
            "    raise ValueError('invalid')"
        )
        sig1 = compute_error_signature(log1)
        sig2 = compute_error_signature(log2)
        self.assertEqual(sig1, sig2)

    def test_protected_paths_include_test_configs_and_git(self) -> None:
        for p in [
            "conftest.py",
            "pytest.ini",
            "sitecustomize.py",
            ".gitignore",
            ".git/config",
            "pyproject.toml",
        ]:
            allowed, reason = evaluate_file_edit(p, cwd=self.wt)
            self.assertFalse(allowed, f"Expected {p} to be protected")
            self.assertIn("受保护", reason)

    async def test_tdd_red_preflight_logic(self) -> None:
        spec_red = SpecContract(
            title="Add feature Y",
            summary="Feature Y",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_y.py",
            acceptance_test_code="def test_y(): assert False\n",
            require_tdd_red=True,
            tasks=[AtomicTask(id="T1", title="Task 1", description="desc", target_files=["src/y.py"])],
        )
        self.assertTrue(spec_red.require_tdd_red)

        spec_no_red = SpecContract(
            title="Refactor Z",
            summary="Refactor Z",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_z.py",
            acceptance_test_code="def test_z(): assert True\n",
            require_tdd_red=False,
            tasks=[AtomicTask(id="T1", title="Task 1", description="desc", target_files=["src/z.py"])],
        )
        self.assertFalse(spec_no_red.require_tdd_red)

    async def test_sync_worktree_dependencies_empty_is_noop(self) -> None:
        ok, msg = await sync_worktree_dependencies(self.wt, self.wt, [])
        self.assertTrue(ok)

    def test_multi_run_tracking_and_active_runs(self) -> None:
        store = StateStore(self.wt)
        store.create_run("run-p2-1")
        store.create_run("run-p2-2")
        store.update_run("run-p2-1", status="RUNNING")
        store.update_run("run-p2-2", status="REPAIRING")

        active = store.get_active_runs()
        self.assertEqual(len(active), 2)
        self.assertTrue(store.is_run_active("run-p2-1"))
        self.assertTrue(store.is_run_active("run-p2-2"))
        self.assertFalse(store.is_run_active("run-non-existent"))

        store.update_run("run-p2-1", status="MR_READY")
        self.assertFalse(store.is_run_active("run-p2-1"))
        self.assertEqual(len(store.get_active_runs()), 1)

    async def test_worker_provider_abstraction(self) -> None:
        mock_p = get_worker_provider("mock")
        self.assertIsInstance(mock_p, MockWorkerProvider)
        res = await mock_p.run_worker(
            worktree=self.wt,
            prompt="do something",
            run_id="run-1",
            allowed_paths=["src/"],
            locked_tests=["tests/test_t.py"],
        )
        self.assertEqual(res["summary"], "Mock completed")


if __name__ == "__main__":
    unittest.main()
