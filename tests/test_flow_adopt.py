from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.state_store import (
    ACTIVE_STATUSES,
    StateStore,
    lifecycle_step,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class TestAdoptedLifecycle(unittest.TestCase):
    """ADOPTED status: state model invariants for the post-hoc adoption channel."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.store = StateStore(self.repo / "state.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_adopted_is_active_status(self) -> None:
        self.assertIn("ADOPTED", ACTIVE_STATUSES)
        self.store.create_run("r1", issue_iid=76, status="ADOPTED", branch="76-doc-adr")
        active = self.store.get_active_runs()
        self.assertTrue(any(r["run_id"] == "r1" for r in active))

    def test_adopted_paints_step_5(self) -> None:
        self.assertEqual(lifecycle_step("ADOPTED"), 5)

    def test_evidence_type_persisted_and_migrated(self) -> None:
        self.store.create_run("r1", issue_iid=1, evidence_type="post_hoc")
        run = self.store.get_run("r1")
        self.assertEqual(run["evidence_type"], "post_hoc")
        # legacy rows created via the default get explicit pipeline evidence
        self.store.create_run("r2", issue_iid=2)
        self.assertEqual(self.store.get_run("r2")["evidence_type"], "pipeline")

    def test_adopt_transitions_cas(self) -> None:
        self.store.create_run("r1", status="ADOPTED")
        # adopt -> MR_READY (finalize)
        ok = self.store.transition_run(
            "r1", expected_statuses=("ADOPTED",), new_status="MR_READY"
        )
        self.assertTrue(ok)
        # adopt -> RUNNING is not admissible (enforce_matrix rejects loudly)
        self.store.create_run("r2", status="ADOPTED")
        with self.assertRaises(ValueError):
            self.store.transition_run(
                "r2",
                expected_statuses=("ADOPTED",),
                new_status="RUNNING",
                enforce_matrix=True,
            )


class TestFlowAdoptService(unittest.IsolatedAsyncioTestCase):
    """flow_adopt / flow_adopt_finalize service path on a real git repo."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init", "-q")
        (self.repo / "README.md").write_text("demo", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-q", "-m", "init demo")
        self.store = StateStore(self.repo / "state.db")
        forge = MagicMock()
        forge.enabled = False
        self.service = FlowService(
            repo_root=self.repo,
            store=self.store,
            forge=forge,
            recovery_grace_period=0.0,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_adopt_registers_post_hoc_run(self) -> None:
        res = await self.service.flow_adopt(issue_iid=76, branch="76-doc-adr", note="ADR 0009")
        self.assertNotIn("error", res)
        self.assertEqual(res["status"], "ADOPTED")
        self.assertEqual(res["evidence_type"], "post_hoc")
        run = self.store.get_run(res["run_id"])
        self.assertEqual(run["branch"], "76-doc-adr")
        self.assertEqual(run["evidence_type"], "post_hoc")
        spec = json.loads(run["spec_json"])
        self.assertTrue(spec["adopted"])

    async def test_adopt_requires_branch(self) -> None:
        res = await self.service.flow_adopt(issue_iid=1, branch="  ")
        self.assertIn("error", res)

    async def test_finalize_records_branch_head_evidence(self) -> None:
        res = await self.service.flow_adopt(issue_iid=76, branch="76-doc-adr")
        done = await self.service.flow_adopt_finalize(run_id=res["run_id"])
        self.assertNotIn("error", done)
        self.assertEqual(done["status"], "MR_READY")
        self.assertEqual(done["evidence_type"], "post_hoc")
        self.assertTrue(done["branch_head"]["short"], "branch head sha must be captured")
        run = self.store.get_run(res["run_id"])
        self.assertEqual(run["status"], "MR_READY")
        self.assertIn("[post_hoc] 收编完成 @", run["last_error_log"])

    async def test_finalize_picks_latest_adopted_run(self) -> None:
        await self.service.flow_adopt(issue_iid=1, branch="1-a")
        await self.service.flow_adopt(issue_iid=2, branch="2-b")
        done = await self.service.flow_adopt_finalize()
        self.assertNotIn("error", done)
        # only the latest adopted run (issue 2) was consumed; the first remains
        self.assertEqual(done["issue_iid"], 2)
        remaining = [r for r in self.store.get_active_runs() if r["status"] == "ADOPTED"]
        self.assertEqual([r["issue_iid"] for r in remaining], [1])

    async def test_finalize_rejects_non_adopted(self) -> None:
        self.store.create_run("r1", issue_iid=1, status="MR_READY")
        done = await self.service.flow_adopt_finalize(run_id="r1")
        self.assertIn("error", done)

    async def test_finalize_without_adopted_returns_error(self) -> None:
        done = await self.service.flow_adopt_finalize()
        self.assertIn("error", done)


if __name__ == "__main__":
    unittest.main()
