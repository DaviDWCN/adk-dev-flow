from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adk_dev_flow.codebuddy_bridge import run_codebuddy_worker
from adk_dev_flow.forge_client import BaseGitForge
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.mutation import evaluate_test_against_mutants_sync
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import CommandResult, run_subprocess_safe
from adk_dev_flow.workflow import build_dev_workflow


class _RecordingOnlineForge(BaseGitForge):
    def __init__(self) -> None:
        self.mr_calls: list[dict] = []
        self.note_calls: list[tuple[int, str]] = []

    @property
    def platform_name(self) -> str:
        return "gitlab"

    @property
    def enabled(self) -> bool:
        return True

    def get_issue(self, issue_iid: int) -> dict:
        return {"iid": issue_iid, "title": "Issue", "description": "Desc"}

    def create_or_update_mr(self, **kwargs) -> dict:
        self.mr_calls.append(kwargs)
        return {"iid": 9, "web_url": "https://gitlab.example.com/mr/9"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        self.note_calls.append((mr_iid, body))

    def mark_mr_ready(self, mr_iid: int) -> dict:
        return {"iid": mr_iid, "ready": True}


def _extract_node(wf, name: str):
    for edge in wf.edges:
        for item in edge:
            if callable(item) and item.__name__ == name:
                return item
            if isinstance(item, (list, tuple)):
                for sub in item:
                    if callable(sub) and sub.__name__ == name:
                        return sub
            if isinstance(item, dict):
                for sub in item.values():
                    if callable(sub) and sub.__name__ == name:
                        return sub
    return None


class TestCoreWorkflowFixes(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "a.py").write_text("def a(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
        (self.repo / "src" / "b.py").write_text("def b(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        self.store = StateStore(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_zero_change_escalates_even_when_setup_commits_precede_locked_commit(self) -> None:
        """P0-1: prepare_worktree_node adds test+spec commits between base_sha and locked_test_commit_sha.

        When the worker makes zero edits after locked_test_commit_sha, verify_python_node must
        still detect zero_change and escalate immediately.
        """
        run_id = "fix-zero-01"
        self.store.create_run(run_id, issue_iid=10)
        spec = SpecContract(
            issue_iid=10,
            title="Zero change with real setup commits",
            summary="Verify locked_test_commit_sha baseline",
            allowed_paths=["src/a.py"],
            acceptance_test_path="tests/test_zero.py",
            acceptance_test_code="def test_z():\n    assert False\n",
            require_tdd_red=False,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="T1", description="D", target_files=["src/a.py"])],
        )
        self.store.update_run(run_id, status="SPEC_LOCKED", spec_json=spec.model_dump_json())

        forge = _RecordingOnlineForge()
        wf = build_dev_workflow(self.repo, self.store, forge, run_id=run_id)
        prepare_node = _extract_node(wf, "prepare_worktree_node")
        verify_node = _extract_node(wf, "verify_python_node")
        self.assertIsNotNone(prepare_node)
        self.assertIsNotNone(verify_node)

        prep_out = await prepare_node(json.dumps({"spec": spec.model_dump()}))
        self.assertNotEqual(prep_out["base_sha"], prep_out["locked_test_commit_sha"])

        real_run = run_subprocess_safe

        async def smart_run(cmd, **kw):
            if cmd and cmd[0] == "git":
                return await real_run(cmd, **kw)
            if cmd and "ruff" in cmd[0]:
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=1, stdout="1 failed", stderr="")

        with (
            patch("adk_dev_flow.workflow.run_subprocess_safe", smart_run),
            patch("adk_dev_flow.worktree_utils.run_subprocess_safe", smart_run),
        ):
            event = await verify_node(prep_out)

        self.assertEqual(event.actions.route, "escalated")
        self.assertIn("零改动", event.output.get("escalation_reason", ""))

    async def test_publish_mr_blocks_git_push_and_mr_when_ast_vetoes(self) -> None:
        """P1-3: When AST gate vetoes (e.g. eval/secret), publish_mr_node must NOT push to origin."""
        run_id = "fix-push-01"
        self.store.create_run(run_id, issue_iid=11)
        spec = SpecContract(
            issue_iid=11,
            title="Block push on AST veto",
            summary="Must not push secret/eval to remote",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_ok.py",
            acceptance_test_code="def test_ok(): assert True\n",
            require_tdd_red=False,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="T1", description="D", target_files=["src/bad.py"])],
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        (self.repo / "src" / "bad.py").write_text(
            "def evil(x):\n    return eval(x)\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "src/bad.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add evil"], cwd=self.repo, check=True, capture_output=True)

        self.store.update_run(
            run_id,
            status="VERIFYING",
            spec_json=spec.model_dump_json(),
            worktree_path=str(self.repo),
            branch="feat/bad-secret",
            base_sha=base_sha,
            locked_test_commit_sha=base_sha,
        )

        forge = _RecordingOnlineForge()
        wf = build_dev_workflow(self.repo, self.store, forge, run_id=run_id)
        publish_node = _extract_node(wf, "publish_mr_node")
        self.assertIsNotNone(publish_node)

        pushed_cmds: list[list[str]] = []
        real_run = run_subprocess_safe

        async def spy_run(cmd, **kw):
            if cmd and cmd[0] == "git" and len(cmd) > 1 and cmd[1] == "push":
                pushed_cmds.append(list(cmd))
                return CommandResult(returncode=0, stdout="", stderr="")
            return await real_run(cmd, **kw)

        with patch("adk_dev_flow.workflow.run_subprocess_safe", spy_run):
            res = await publish_node({"worktree": str(self.repo), "spec": spec.model_dump(), "attempt": 0})

        self.assertEqual(res.get("status"), "ESCALATED")
        self.assertEqual(pushed_cmds, [], "Rejected code must NOT be pushed to remote origin")
        self.assertEqual(forge.mr_calls, [], "Rejected code must NOT create remote PR/MR")

    def test_mutation_gate_evaluates_all_target_files_and_rejects_red_baseline_fake_kill(self) -> None:
        """P0-2: Secondary target files must not be skipped, and failing baseline tests must not get 100% kill ratio."""
        # 1. Baseline failing test must NOT be treated as killing mutants
        report = evaluate_test_against_mutants_sync(
            target_source="def add(x, y):\n    return x + y\n",
            test_source="def test_broken():\n    assert False\n",
            target_module_name="mod",
            min_kill_ratio=0.85,
        )
        self.assertFalse(report.is_acceptable, "Failing baseline test must not pass mutation gate")
        self.assertEqual(report.kill_ratio, 0.0)

        # 2. When require_tdd_red=True, flow_lock_spec must skip baseline mutation gate (no fake kill report)
        service = FlowService(repo_root=self.repo, store=self.store)
        run_id = "fix-mut-red"
        self.store.create_run(run_id, issue_iid=12)
        spec_red = SpecContract(
            issue_iid=12,
            title="TDD Red skips baseline mutation",
            summary="Skip fake baseline mutation when require_tdd_red=True",
            allowed_paths=["src/a.py"],
            acceptance_test_path="tests/test_a.py",
            acceptance_test_code="from src.a import a\ndef test_a(): assert a(1) == 999\n",
            require_tdd_red=True,
            require_mutation_gate=True,
            tasks=[AtomicTask(id="T1", title="T1", description="D", target_files=["src/a.py"])],
        )
        res_red = service.flow_lock_spec(run_id, spec_red, strict_prelock=False)
        self.assertEqual(res_red.get("status"), "SPEC_LOCKED")
        self.assertNotIn("mutation_report", res_red)

        # 3. When require_tdd_red=False, flow_lock_spec checks ALL target files (not only target_sources[0])
        run_id_2 = "fix-mut-multi"
        self.store.create_run(run_id_2, issue_iid=13)
        # Strong test for src/a.py, but ZERO assertions on src/b.py!
        test_only_a = (
            "from src.a import a\n"
            "from src.b import b\n"
            "def test_a_strict():\n"
            "    assert a(0) == 1\n"
            "    assert a(5) == 6\n"
            "def test_b_weak():\n"
            "    assert b(2) is not None\n"
        )
        spec_multi = SpecContract(
            issue_iid=13,
            title="Check all target files",
            summary="Must catch weak coverage on second target file src/b.py",
            allowed_paths=["src/a.py", "src/b.py"],
            acceptance_test_path="tests/test_ab.py",
            acceptance_test_code=test_only_a,
            require_tdd_red=False,
            require_mutation_gate=True,
            tasks=[
                AtomicTask(
                    id="T1",
                    title="T1",
                    description="D",
                    target_files=["src/a.py", "src/b.py"],
                )
            ],
        )
        res_multi = service.flow_lock_spec(run_id_2, spec_multi, strict_prelock=False)
        self.assertIn("error", res_multi, "Weak coverage on second target file src/b.py must be rejected")

    async def test_codebuddy_worker_passes_port_flag_only_once(self) -> None:
        """P1-4: run_codebuddy_worker must not emit duplicate --port flags."""
        captured_cmd: list[str] = []

        async def fake_run(cmd, cwd, env=None, stdin_text=None, timeout_sec=None):
            nonlocal captured_cmd
            captured_cmd = list(cmd)
            return CommandResult(returncode=0, stdout='{"changed_files": [], "summary": "ok"}', stderr="")

        with patch("adk_dev_flow.codebuddy_bridge.run_subprocess_safe", side_effect=fake_run):
            await run_codebuddy_worker(
                worktree=self.repo,
                prompt="do work",
                run_id="run-port-once",
                allowed_paths=["src/"],
                locked_tests=["tests/"],
            )

        port_count = sum(1 for arg in captured_cmd if arg == "--port" or arg.startswith("--port="))
        self.assertEqual(port_count, 1, f"Expected exactly 1 --port flag, got {captured_cmd}")

    def test_workflow_reads_max_retries_and_git_timeout_at_call_time(self) -> None:
        """P2: get_max_repair_attempts and get_git_timeout_sec must reflect runtime env changes."""
        from adk_dev_flow.workflow import get_git_timeout_sec, get_max_repair_attempts

        with patch.dict(os.environ, {"ADK_MAX_RETRIES": "7", "ADK_GIT_TIMEOUT": "95"}, clear=False):
            self.assertEqual(get_max_repair_attempts(), 7)
            self.assertEqual(get_git_timeout_sec(), 95.0)
