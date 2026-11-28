from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.worktree_utils import sanitize_worktree_state


class TestCrashConsistencyFaultInjection(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {"ADK_WORKER_PROVIDER": "mock"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp_dir.name)

        # Initialize real git repository
        import subprocess

        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.repo_root), check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "Tester"], cwd=str(self.repo_root), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "tester@example.com"],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )

        # Commit initial baseline file
        init_file = self.repo_root / "README.md"
        init_file.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_root), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: initial commit"],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )

        self.db_path = self.repo_root / ".adk" / "state.db"
        self.store = StateStore(self.db_path)
        self.mock_forge = MagicMock()
        self.mock_forge.enabled = False
        self.service = FlowService(repo_root=self.repo_root, store=self.store, forge=self.mock_forge)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def test_crash_during_sandbox_creation_recovery(self) -> None:
        """Requirement: A half-baked worktree from an interrupted sandbox initialization

        must be detected, pruned, and cleanly rebuilt upon flow_resume.
        """
        init_res = await self.service.flow_init(requirement="Crash recovery test")
        run_id = init_res["run_id"]

        test_code = """
def test_dummy():
    assert True
"""
        spec = SpecContract(
            title="Fault Injection Spec",
            summary="Test crash recovery",
            allowed_paths=["src/module.py"],
            acceptance_test_path="tests/test_crash.py",
            acceptance_test_code=test_code,
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task 1",
                    description="Implement",
                    target_files=["src/module.py"],
                )
            ],
            require_tdd_red=False,
            require_mutation_gate=False,
        )

        lock_res = self.service.flow_lock_spec(run_id, spec, strict_prelock=False)
        self.assertEqual(lock_res["status"], "SPEC_LOCKED")

        # Simulate fault: create a half-baked worktree directory (corrupted, no .git file)
        wt_dir = self.repo_root / ".adk" / "worktrees" / run_id
        wt_dir.mkdir(parents=True, exist_ok=True)
        (wt_dir / "incomplete_file.txt").write_text("crashed halfway")

        # Simulate system crash while in RUNNING
        self.store.update_run(
            run_id,
            status="RUNNING",
            worktree_path=str(wt_dir),
            branch=f"adk/issue-task-{run_id[:6]}",
        )

        # 1. System reboots: FlowService starts up (grace period 0: the crash
        #    happened long ago, do not treat the RUNNING run as still alive)
        reboot_service = FlowService(
            repo_root=self.repo_root, store=self.store, forge=self.mock_forge, recovery_grace_period=0.0
        )
        self.assertIn(run_id, reboot_service.recovered_runs)

        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "ESCALATED")
        self.assertIn("[系统恢复]", run["last_error_log"])

        # 2. Resume execution: prepare_worktree_node should detect invalid worktree,
        # clean it up, and successfully create a valid git worktree
        resume_res = await reboot_service.flow_resume(run_id, instruction="Resume after crash")
        self.assertEqual(resume_res["status"], "RUNNING")

        # Await the execution task
        task = reboot_service.bg_tasks.get(run_id)
        if task:
            await task

        run_after = self.store.get_run(run_id)
        # Worktree should now be a valid directory containing a valid .git link file
        self.assertTrue(wt_dir.exists())
        self.assertTrue((wt_dir / ".git").is_file())
        self.assertIn(run_after["status"], ("VERIFYING", "MR_READY", "REPAIRING", "ESCALATED"))

    async def test_crash_with_stale_index_lock_sanitized(self) -> None:
        """Requirement: Stale index.lock files left by killed git processes must be cleanly removed."""
        wt_dir = self.repo_root / ".adk" / "worktrees" / "test_wt"
        wt_dir.mkdir(parents=True, exist_ok=True)

        # Mock worktree .git file pointing to actual gitdir
        git_dir = self.repo_root / ".git" / "worktrees" / "test_wt"
        git_dir.mkdir(parents=True, exist_ok=True)
        (wt_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

        # Inject stale index.lock in the worktree gitdir
        stale_lock = git_dir / "index.lock"
        stale_lock.write_text("locked", encoding="utf-8")
        self.assertTrue(stale_lock.is_file())

        # Run sanitization
        await sanitize_worktree_state(wt_dir)

        # Lock must be removed
        self.assertFalse(stale_lock.exists(), "Stale index.lock should be cleaned up")

    async def test_abort_crashed_run_cleans_hanging_worktree_and_locks(self) -> None:
        """Requirement: flow_abort on an interrupted run must remove worktree and clear active markers."""
        init_res = await self.service.flow_init(requirement="Abort crash test")
        run_id = init_res["run_id"]

        wt_dir = self.repo_root / ".adk" / "worktrees" / run_id
        wt_dir.mkdir(parents=True, exist_ok=True)
        (wt_dir / "abandoned.txt").write_text("abandoned")

        self.store.update_run(
            run_id,
            status="RUNNING",
            worktree_path=str(wt_dir),
        )

        abort_res = await self.service.flow_abort(run_id, remove_worktree=True)
        self.assertEqual(abort_res["status"], "ABORTED")

        # Verify worktree is removed
        self.assertFalse(wt_dir.exists(), "Worktree directory must be deleted on abort")

        # Verify active run marker is cleared
        active_runs = self.service.flow_active_runs()
        self.assertEqual(active_runs["count"], 0)
