from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.state_store import StateStore


class TestStateStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name).resolve()
        self.store = StateStore(self.repo_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_wal_mode_enabled(self) -> None:
        with self.store._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_attempt_logs_roundtrip_and_ordering(self) -> None:
        """Per-round self-heal evidence must persist and read back latest-first.

        Lesson: run 558a138f54eb — only the last round survived in the runs row,
        making signature-drift post-mortems impossible.
        """
        run_id = "logs-run-001"
        self.store.create_run(run_id, issue_iid=1)
        self.store.log_attempt(
            run_id,
            0,
            err_sig="sig-round0",
            combined_err="[pytest exit=1]\nImportError: boom",
            worker_summary="explored the repo",
            changed_files=[],
        )
        self.store.log_attempt(
            run_id,
            1,
            err_sig="sig-round1",
            combined_err="[pytest exit=1]\nImportError: boom",
            worker_summary="wrote src/x.py",
            changed_files=["src/x.py"],
        )
        self.store.log_attempt(run_id, 2, worker_summary="green round")

        logs = self.store.list_attempt_logs(run_id, limit=5)
        self.assertEqual([l["attempt"] for l in logs], [2, 1, 0])
        self.assertEqual(logs[0]["worker_summary"], "green round")
        self.assertIsNone(logs[0]["err_sig"])
        self.assertEqual(logs[1]["err_sig"], "sig-round1")
        self.assertEqual(json.loads(logs[1]["changed_files"]), ["src/x.py"])
        self.assertIn("ImportError", logs[1]["combined_err"])

        limited = self.store.list_attempt_logs(run_id, limit=2)
        self.assertEqual([l["attempt"] for l in limited], [2, 1])

        # Logs are scoped per run
        self.store.create_run("logs-run-002", issue_iid=2)
        self.assertEqual(self.store.list_attempt_logs("logs-run-002"), [])

    def test_v1_schema_auto_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adk_dir = root / ".adk"
            adk_dir.mkdir()
            db_path = adk_dir / "workflow.db"
            # Create legacy v1 schema without base_sha / locked_test_sha256 / human_hint
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    issue_iid INTEGER,
                    spec_json TEXT,
                    branch TEXT,
                    worktree_path TEXT,
                    mr_iid INTEGER,
                    mr_url TEXT,
                    attempt INTEGER DEFAULT 0,
                    last_error_sig TEXT,
                    last_error_log TEXT,
                    review_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            migrated = StateStore(root)
            migrated.create_run("r1")
            migrated.update_run(
                "r1",
                base_sha="deadbeef",
                locked_test_sha256="abc123",
                locked_test_commit_sha="cafebabe",
                human_hint="check edge case",
            )
            row = migrated.get_run("r1")
            assert row is not None
            self.assertEqual(row["base_sha"], "deadbeef")
            self.assertEqual(row["human_hint"], "check edge case")

    def test_cas_transition_prevents_duplicate_dispatch(self) -> None:
        self.store.create_run("run-cas")
        self.assertTrue(
            self.store.transition_run(
                "run-cas", expected_statuses=("SPEC_DRAFTING",), new_status="SPEC_LOCKED"
            )
        )
        # First dispatch succeeds
        self.assertTrue(
            self.store.transition_run(
                "run-cas", expected_statuses=("SPEC_LOCKED",), new_status="RUNNING"
            )
        )
        # Second concurrent dispatch fails atomically
        self.assertFalse(
            self.store.transition_run(
                "run-cas", expected_statuses=("SPEC_LOCKED",), new_status="RUNNING"
            )
        )

    def test_formal_transition_matrix_enforcement(self) -> None:
        from adk_dev_flow.state_store import is_valid_transition

        self.assertTrue(is_valid_transition("SPEC_LOCKED", "RUNNING"))
        self.assertTrue(is_valid_transition("RUNNING", "VERIFYING"))
        self.assertTrue(is_valid_transition("REVIEWING", "MR_READY"))
        self.assertFalse(is_valid_transition("SPEC_DRAFTING", "MR_READY"))
        self.assertFalse(is_valid_transition("RUNNING", "MR_READY"))

        self.store.create_run("run-matrix")
        # Enforcing transition matrix rejects illegal leap: SPEC_DRAFTING -> MR_READY
        with self.assertRaises(ValueError):
            self.store.transition_run(
                "run-matrix",
                expected_statuses=("SPEC_DRAFTING",),
                new_status="MR_READY",
                enforce_matrix=True,
            )

    def test_invalid_column_rejected(self) -> None:
        self.store.create_run("r-col")
        with self.assertRaises(ValueError):
            self.store.update_run("r-col", unknown_column="boom")

    def test_recover_orphaned_runs_and_list_runs(self) -> None:
        self.store.create_run("r-draft")
        self.store.create_run("r-run")
        self.store.update_run("r-run", status="RUNNING")
        self.store.create_run("r-repair")
        self.store.update_run("r-repair", status="REPAIRING")
        self.store.create_run("r-done")
        self.store.update_run("r-done", status="MR_READY")

        recovered = self.store.recover_orphaned_runs()
        self.assertEqual(set(recovered), {"r-run", "r-repair"})

        self.assertEqual(self.store.get_run("r-run")["status"], "ESCALATED")  # type: ignore[index]
        self.assertEqual(self.store.get_run("r-repair")["status"], "ESCALATED")  # type: ignore[index]
        self.assertEqual(self.store.get_run("r-draft")["status"], "SPEC_DRAFTING")  # type: ignore[index]
        self.assertEqual(self.store.get_run("r-done")["status"], "MR_READY")  # type: ignore[index]

        listed = self.store.list_runs(limit=10)
        self.assertEqual(len(listed), 4)

    def test_active_marker_lifecycle(self) -> None:
        self.store.create_run("r1")
        self.assertTrue(self.store.active_marker.exists())
        marker_data = json.loads(self.store.active_marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_data["run_id"], "r1")

        self.store.update_run("r1", status="MR_READY")
        self.assertFalse(self.store.active_marker.exists())


if __name__ == "__main__":
    unittest.main()
