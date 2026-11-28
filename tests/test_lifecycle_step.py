from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import get_args

from adk_dev_flow.dashboard import _build_run_payload, get_dashboard_html
from adk_dev_flow.state_store import (
    ACTIVE_STATUSES,
    IN_FLIGHT_STATUSES,
    LIFECYCLE_STEP,
    RunStatus,
    StateStore,
    lifecycle_step,
)


_STATUS_ASSIGN = re.compile(r'(?:status|new_status)="([A-Z_]+)"')
_GHOST_KEYS = (
    "SPEC_DESIGN",
    "RED_TEST",
    "PRE_CRITIC",
    "HITL_LOCKED",
    "BUILDING",
    "RETRYING",
    "COMPLETED",
)


class TestLifecycleStep(unittest.TestCase):
    def test_table_covers_every_run_status(self) -> None:
        declared = set(get_args(RunStatus))
        self.assertEqual(set(LIFECYCLE_STEP), declared)
        for status in declared:
            self.assertEqual(lifecycle_step(status), LIFECYCLE_STEP[status])

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            lifecycle_step("HITL_LOCKED")

    def test_written_statuses_belong_to_run_status(self) -> None:
        src = Path(__file__).resolve().parents[1] / "src"
        written: set[str] = set()
        for path in src.rglob("*.py"):
            written.update(_STATUS_ASSIGN.findall(path.read_text(encoding="utf-8")))
        self.assertEqual(written - set(get_args(RunStatus)), set())

    def test_spec_locked_payload_is_step_four(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            store.create_run("run-locked")
            store.update_run("run-locked", status="SPEC_LOCKED")
            payload = _build_run_payload(store.get_run("run-locked"), "repo")
        self.assertEqual(payload["status"], "SPEC_LOCKED")
        self.assertEqual(payload["step"], 4)
        self.assertFalse(payload["highlight_critic"])

    def test_reviewing_stays_on_step_six_without_ai_critic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            store.create_run("run-review")
            store.update_run("run-review", status="REVIEWING")
            payload = _build_run_payload(store.get_run("run-review"), "repo")
            store.create_run("run-pre-critic")
            store.update_run("run-pre-critic", status="PRE_CRITIC_REVIEWED")
            pre_payload = _build_run_payload(store.get_run("run-pre-critic"), "repo")
        self.assertEqual(payload["step"], 6)
        self.assertFalse(payload["highlight_critic"])
        self.assertEqual(pre_payload["step"], 3)
        self.assertTrue(pre_payload["highlight_critic"])

    def test_verifying_is_in_flight_and_recovered(self) -> None:
        self.assertIn("VERIFYING", ACTIVE_STATUSES)
        self.assertIn("VERIFYING", IN_FLIGHT_STATUSES)
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            store.create_run("run-verify")
            store.update_run("run-verify", status="VERIFYING")
            self.assertTrue(store.active_marker.exists())
            recovered = store.recover_orphaned_runs()
            self.assertEqual(recovered, ["run-verify"])
            run = store.get_run("run-verify")
            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run["status"], "ESCALATED")

    def test_aborted_is_not_delivery(self) -> None:
        self.assertEqual(lifecycle_step("ABORTED"), 0)
        self.assertEqual(lifecycle_step("MR_READY"), 8)

    def test_idle_payload_stays_idle(self) -> None:
        payload = _build_run_payload(None, "repo")
        self.assertEqual(payload["status"], "IDLE")
        self.assertEqual(payload["step"], 0)
        self.assertFalse(payload["highlight_critic"])

    def test_dashboard_reads_step_and_drops_ghost_names(self) -> None:
        html = get_dashboard_html()
        self.assertIn("data.step", html)
        self.assertIn("highlight_critic", html)
        self.assertNotIn("stageMap", html)
        for key in _GHOST_KEYS:
            self.assertNotIn(f"'{key}'", html)


if __name__ == "__main__":
    unittest.main()
