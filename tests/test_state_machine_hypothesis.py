from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from adk_dev_flow.state_store import (
    ACTIVE_STATUSES,
    PRE_LOCK_STATUSES,
    RunStatus,
    StateStore,
    is_valid_transition,
)


class WorkflowFuzzStateMachine(RuleBasedStateMachine):
    """Hypothesis stateful fuzzer exploring arbitrary interleaved state machine sequences."""

    runs = Bundle("runs")

    def __init__(self) -> None:
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp.name)
        self.store = StateStore(self.repo_root)
        self.model_state: dict[str, dict] = {}
        self.run_counter = 0

    def teardown(self) -> None:
        self.tmp.cleanup()

    @rule(target=runs)
    def create_run(self) -> str:
        self.run_counter += 1
        run_id = f"fuzz-run-{self.run_counter}"
        self.store.create_run(run_id, issue_iid=self.run_counter)
        self.model_state[run_id] = {
            "status": "SPEC_DRAFTING",
            "has_locked": False,
            "has_verified": False,
            "attempt": 0,
        }
        return run_id

    @rule(run_id=runs)
    def progress_design(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "SPEC_DESIGNED"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=[current],
                new_status="SPEC_DESIGNED",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "SPEC_DESIGNED"

    @rule(run_id=runs)
    def generate_red_test(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "TEST_GENERATED"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=[current],
                new_status="TEST_GENERATED",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "TEST_GENERATED"

    @rule(run_id=runs)
    def pre_critic_review(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "PRE_CRITIC_REVIEWED"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=[current],
                new_status="PRE_CRITIC_REVIEWED",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "PRE_CRITIC_REVIEWED"

    @rule(run_id=runs)
    def lock_spec(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "SPEC_LOCKED"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=PRE_LOCK_STATUSES,
                new_status="SPEC_LOCKED",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "SPEC_LOCKED"
                self.model_state[run_id]["has_locked"] = True

    @rule(run_id=runs)
    def dispatch_worker(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "RUNNING"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=["SPEC_LOCKED"],
                new_status="RUNNING",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "RUNNING"

    @rule(run_id=runs)
    def verify_physical(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "VERIFYING"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=["RUNNING", "REPAIRING"],
                new_status="VERIFYING",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "VERIFYING"
                self.model_state[run_id]["has_verified"] = True

    @rule(run_id=runs)
    def repair_cycle(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "REPAIRING"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=["VERIFYING"],
                new_status="REPAIRING",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "REPAIRING"
                self.model_state[run_id]["attempt"] += 1

    @rule(run_id=runs)
    def deliver_mr_direct(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "MR_READY"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=["VERIFYING"],
                new_status="MR_READY",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "MR_READY"

    @rule(run_id=runs)
    def escalate_failure(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "ESCALATED"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=["VERIFYING", "REPAIRING", "RUNNING"],
                new_status="ESCALATED",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "ESCALATED"

    @rule(run_id=runs)
    def abort_pipeline(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "ABORTED"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=[current],
                new_status="ABORTED",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "ABORTED"

    @rule(run_id=runs)
    def resume_escalated(self, run_id: str) -> None:
        current = self.model_state[run_id]["status"]
        if is_valid_transition(current, "RUNNING"):
            ok = self.store.transition_run(
                run_id,
                expected_statuses=["ESCALATED"],
                new_status="RUNNING",
                enforce_matrix=True,
            )
            if ok:
                self.model_state[run_id]["status"] = "RUNNING"

    @invariant()
    def invariant_database_matches_model(self) -> None:
        """Every run's state in SQLite must match model_state exactly."""
        for run_id, meta in self.model_state.items():
            db_run = self.store.get_run(run_id)
            assert db_run is not None, f"Run {run_id} must exist in DB"
            assert db_run["status"] == meta["status"], (
                f"Run {run_id} status mismatch: DB={db_run['status']}, Model={meta['status']}"
            )

    @invariant()
    def invariant_mr_ready_strict_prior_lock_and_verify(self) -> None:
        """Formal Invariant: Any run reaching MR_READY MUST have passed SPEC_LOCKED and VERIFYING."""
        for run_id, meta in self.model_state.items():
            if meta["status"] == "MR_READY":
                assert meta["has_locked"], f"Run {run_id} reached MR_READY without being locked!"
                assert meta["has_verified"], f"Run {run_id} reached MR_READY without being verified!"

    @invariant()
    def invariant_active_marker_consistency(self) -> None:
        """Active marker must never record terminated runs (MR_READY, ABORTED, ESCALATED)."""
        marker = self.store.read_active_marker()
        if marker:
            active_ids = marker.get("active_run_ids", [])
            for aid in active_ids:
                if aid in self.model_state:
                    assert self.model_state[aid]["status"] not in ("MR_READY", "ABORTED", "ESCALATED"), (
                        f"Active marker recorded terminal run {aid} in status {self.model_state[aid]['status']}"
                    )


class TestHypothesisFuzzing(unittest.TestCase):
    def test_state_machine_concurrency_invariants(self) -> None:
        run_state_machine_as_test(
            WorkflowFuzzStateMachine,
            settings=settings(max_examples=70, stateful_step_count=80, deadline=None),
        )


if __name__ == "__main__":
    unittest.main()
