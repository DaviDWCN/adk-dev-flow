from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.forge_client import BaseGitForge, GitLabClient
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import build_dev_workflow, execute_prelock_workflow


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=tester@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _extract_node(workflow, name: str):
    for edge in workflow.edges:
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


class _FailingMrAfterPushForge(BaseGitForge):
    def __init__(self, *, fail_mr: bool = True) -> None:
        self.fail_mr = fail_mr
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
        if self.fail_mr:
            raise RuntimeError("502 Bad Gateway from GitLab")
        return {"iid": 7, "web_url": "https://gitlab.example.com/mr/7"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        self.note_calls.append((mr_iid, body))

    def mark_mr_ready(self, mr_iid: int) -> dict:
        return {"iid": mr_iid, "ready": True}


class TestPreLockAndDeliveryHardeningRound2(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bare = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.bare.mkdir()
        self.repo.mkdir()
        _git(self.bare, "init", "--bare")
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "remote", "add", "origin", str(self.bare))
        (self.repo / "src" / "mypkg").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "mypkg" / "__init__.py").write_text(
            "def solve(x: int) -> int:\n    return 0\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "chore: baseline")
        _git(self.repo, "push", "-u", "origin", "main")
        self.store = StateStore(self.repo)
        self.forge = GitLabClient()
        self.service = FlowService(self.repo, self.store, self.forge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_preflight_rerun_after_pre_critic_invalidates_approval_and_blocks_lock(self) -> None:
        """Re-running flow_preflight_test after PRE_CRITIC_REVIEWED must revert status and clear stale approval."""
        run_id = (await self.service.flow_init("Re-preflight invalidates pre-critic"))["run_id"]
        spec_a = SpecContract(
            title="Solve feature",
            summary="Solve returns x * 2",
            design_doc="Multiply input by 2.",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve(2) == 4\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )

        with patch.dict(os.environ, {"ADK_WORKER_PROVIDER": "mock"}, clear=False):
            self.service.flow_analyze_impact(["src/mypkg/__init__.py"], run_id=run_id)
            self.service.flow_preflight_test(spec_a.acceptance_test_path, spec_a.acceptance_test_code, run_id=run_id)
            pc_a = await self.service.flow_pre_critic(run_id=run_id, spec=spec_a)
            self.assertTrue(pc_a["approved"])
            self.assertEqual(self.store.get_run(run_id)["status"], "PRE_CRITIC_REVIEWED")

            # Now test is edited and preflighted again -> old PRE_CRITIC_REVIEWED approval is stale!
            spec_b = spec_a.model_copy(
                update={
                    "acceptance_test_code": "from src.mypkg import solve\ndef test_solve():\n    assert solve(3) == 6\n"
                }
            )
            red_b = self.service.flow_preflight_test(
                spec_b.acceptance_test_path, spec_b.acceptance_test_code, run_id=run_id
            )
            self.assertTrue(red_b["ok"], red_b)
            run_after = self.store.get_run(run_id)
            self.assertEqual(run_after["status"], "TEST_GENERATED")
            prelock_after = self.store.get_prelock_data(run_id)
            self.assertIsNone(prelock_after.get("pre_critic_report"))

            # Locking before re-running flow_pre_critic must be blocked
            blocked = self.service.flow_lock_spec(run_id, spec_b)
            self.assertIn("error", blocked)

            # Re-run Step 3 Critic on spec_b, then design_doc edits afterwards must also be caught by spec_digest
            pc_b = await self.service.flow_pre_critic(run_id=run_id, spec=spec_b)
            self.assertTrue(pc_b["approved"])
            tampered_design = spec_b.model_copy(update={"design_doc": "Completely changed architecture!"})
            blocked_design = self.service.flow_lock_spec(run_id, tampered_design)
            self.assertIn("error", blocked_design)

            # Unmodified spec_b locks cleanly
            ok_lock = self.service.flow_lock_spec(run_id, spec_b)
            self.assertEqual(ok_lock.get("status"), "SPEC_LOCKED")

    async def test_flow_pre_critic_from_file_supports_okf_markdown_pipeline(self) -> None:
        """Users using OKF Markdown specs must be able to run flow_pre_critic_from_file -> flow_lock_spec_from_file."""
        run_id = (await self.service.flow_init("OKF file Pre-Critic"))["run_id"]
        spec = SpecContract(
            issue_iid=42,
            title="OKF pre-critic file flow",
            summary="Verify end-to-end OKF Markdown flow with strict prelock",
            design_doc="Return x * 2.",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve(2) == 4\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )
        spec_file = self.repo / ".adk" / "specs" / "issue-42.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(spec.to_okf_markdown(), encoding="utf-8")

        with patch.dict(os.environ, {"ADK_WORKER_PROVIDER": "mock"}, clear=False):
            self.service.flow_analyze_impact(["src/mypkg/__init__.py"], run_id=run_id)
            self.service.flow_preflight_test(spec.acceptance_test_path, spec.acceptance_test_code, run_id=run_id)
            pc_res = await self.service.flow_pre_critic_from_file(run_id=run_id, spec_file_path=".adk/specs/issue-42.md")
            self.assertTrue(pc_res["approved"], pc_res)
            self.assertEqual(self.store.get_run(run_id)["status"], "PRE_CRITIC_REVIEWED")

            lock_res = self.service.flow_lock_spec_from_file(run_id=run_id, spec_file_path=".adk/specs/issue-42.md")
            self.assertEqual(lock_res.get("status"), "SPEC_LOCKED")

    async def test_execute_prelock_workflow_propagates_preflight_error(self) -> None:
        """When ADK Pre-Lock workflow fails in preflight (mock pass test), execute_prelock_workflow must return error."""
        run_id = (await self.service.flow_init("Failing preflight propagation"))["run_id"]
        spec = SpecContract(
            title="Fake passing test",
            summary="Should fail preflight and surface error",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_fake.py",
            acceptance_test_code="def test_fake():\n    assert True\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )
        with patch.dict(os.environ, {"ADK_WORKER_PROVIDER": "mock"}, clear=False):
            res = await execute_prelock_workflow(self.repo, self.store, run_id=run_id, spec=spec)
        self.assertIn("error", res)
        self.assertTrue(res["error"])
        self.assertNotEqual(res["status"], "PRE_CRITIC_REVIEWED")

    async def test_flow_preflight_test_installs_extra_dependencies_in_isolated_worktree(self) -> None:
        """flow_preflight_test must accept extra_dependencies and sync them in the isolated worktree."""
        run_id = (await self.service.flow_init("Preflight extra deps"))["run_id"]
        captured_deps: list[tuple[str, list[str]]] = []

        def fake_sync(worktree, repo_root, extra_deps=()):
            captured_deps.append((str(worktree.resolve()), list(extra_deps)))
            return True, "synced"

        with patch("adk_dev_flow.workflow.sync_worktree_dependencies_sync", side_effect=fake_sync):
            res = self.service.flow_preflight_test(
                "tests/test_dep.py",
                "def test_dep():\n    assert False\n",
                run_id=run_id,
                extra_dependencies=["redis>=5.0.0"],
            )
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(captured_deps), 1)
        self.assertEqual(captured_deps[0][1], ["redis>=5.0.0"])
        self.assertNotEqual(captured_deps[0][0], str(self.repo.resolve()))

    async def test_publish_mr_preserves_head_sha_on_mr_create_failure_and_uses_step8_wording(self) -> None:
        """When git push succeeds but create_or_update_mr fails, head_sha must be recorded and Step 8 wording updated."""
        run_id = "push-mr-fail-01"
        self.store.create_run(run_id, issue_iid=21)
        spec = SpecContract(
            issue_iid=21,
            title="MR API failure after push",
            summary="Preserve head_sha and clear message",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="def test_solve():\n    assert True\n",
            require_tdd_red=False,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Edit", target_files=["src/mypkg/__init__.py"])],
        )
        self.store.update_run(run_id, status="SPEC_LOCKED", spec_json=spec.model_dump_json())

        failing_forge = _FailingMrAfterPushForge(fail_mr=True)
        wf = build_dev_workflow(self.repo, self.store, failing_forge, run_id=run_id)
        prep_node = _extract_node(wf, "prepare_worktree_node")
        publish_node = _extract_node(wf, "publish_mr_node")
        prep_out = await prep_node(json.dumps({"spec": spec.model_dump()}))

        wt = Path(prep_out["worktree"])
        (wt / "src" / "mypkg" / "__init__.py").write_text("def solve(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
        _git(wt, "add", "src/mypkg/__init__.py")
        _git(wt, "commit", "-m", "feat: return x*2")
        self.store.update_run(run_id, status="VERIFYING")

        res = await publish_node(prep_out)
        self.assertEqual(res["status"], "ESCALATED")
        run_row = self.store.get_run(run_id)
        self.assertEqual(run_row["status"], "ESCALATED")
        self.assertTrue(run_row.get("head_sha"), "head_sha must be recorded after git push even when MR API fails")
        self.assertIn("已推送", run_row["last_error_log"])

        # Now test successful MR creation -> verify clean Step 8 deterministic wording (not Critic)
        ok_forge = _FailingMrAfterPushForge(fail_mr=False)
        wf2 = build_dev_workflow(self.repo, self.store, ok_forge, run_id=run_id)
        publish_node2 = _extract_node(wf2, "publish_mr_node")
        self.store.update_run(run_id, status="VERIFYING")
        res2 = await publish_node2(prep_out)
        self.assertEqual(res2["status"], "MR_READY")
        self.assertNotIn("独立 Critic", ok_forge.mr_calls[0]["description"])
        self.assertNotIn("ADK Critic", ok_forge.note_calls[0][1])
