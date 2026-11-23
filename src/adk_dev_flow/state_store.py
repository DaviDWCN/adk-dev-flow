from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence

RunStatus = Literal[
    "SPEC_DRAFTING",
    "SPEC_LOCKED",
    "RUNNING",
    "REPAIRING",
    "REVIEWING",
    "MR_READY",
    "ESCALATED",
    "ABORTED",
]

ACTIVE_STATUSES: tuple[str, ...] = (
    "SPEC_DRAFTING",
    "SPEC_LOCKED",
    "RUNNING",
    "REPAIRING",
    "REVIEWING",
)

IN_FLIGHT_STATUSES: tuple[str, ...] = (
    "RUNNING",
    "REPAIRING",
    "REVIEWING",
)

ALLOWED_RUN_COLUMNS: frozenset[str] = frozenset(
    {
        "status",
        "issue_iid",
        "spec_json",
        "branch",
        "worktree_path",
        "base_sha",
        "locked_test_sha256",
        "locked_test_commit_sha",
        "mr_iid",
        "mr_url",
        "attempt",
        "last_error_sig",
        "last_error_log",
        "review_json",
        "human_hint",
        "updated_at",
    }
)


class StateStore:
    """SQLite-backed workflow state store with WAL mode, CAS transitions, and active-run tracking."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.adk_dir = self.repo_root / ".adk"
        self.adk_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.adk_dir / "workflow.db"
        self.active_marker = self.adk_dir / "active_run.json"
        self._init_db()

    @property
    def marker_path(self) -> Path:
        return self.active_marker

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    issue_iid INTEGER,
                    spec_json TEXT,
                    branch TEXT,
                    worktree_path TEXT,
                    base_sha TEXT,
                    locked_test_sha256 TEXT,
                    locked_test_commit_sha TEXT,
                    mr_iid INTEGER,
                    mr_url TEXT,
                    attempt INTEGER DEFAULT 0,
                    last_error_sig TEXT,
                    last_error_log TEXT,
                    review_json TEXT,
                    human_hint TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Auto-migrate older v1 SQLite schemas cleanly.
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            migrations = {
                "base_sha": "TEXT",
                "locked_test_sha256": "TEXT",
                "locked_test_commit_sha": "TEXT",
                "human_hint": "TEXT",
            }
            for col, col_type in migrations.items():
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type}")

    @staticmethod
    def _validate_fields(fields: dict[str, Any]) -> None:
        invalid = set(fields.keys()) - ALLOWED_RUN_COLUMNS
        if invalid:
            raise ValueError(f"非法的 runs 更新字段: {sorted(invalid)}")

    def create_run(self, run_id: str, issue_iid: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, status, issue_iid, updated_at) VALUES (?, ?, ?, ?)",
                (run_id, "SPEC_DRAFTING", issue_iid, now),
            )
        self._sync_active_marker()

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._validate_fields(fields)
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [run_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", vals)
        if "status" in fields:
            self._sync_active_marker()

    def transition_run(
        self,
        run_id: str,
        *,
        expected_statuses: Sequence[str],
        new_status: RunStatus,
        **fields: Any,
    ) -> bool:
        """Atomic compare-and-swap (CAS) state transition. Returns True iff transition succeeded."""
        if not expected_statuses:
            raise ValueError("expected_statuses 不能为空")
        update_fields: dict[str, Any] = dict(fields)
        update_fields["status"] = new_status
        update_fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._validate_fields(update_fields)

        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
        in_clause = ", ".join("?" for _ in expected_statuses)
        vals = list(update_fields.values()) + [run_id, *expected_statuses]

        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE runs SET {set_clause} WHERE run_id = ? AND status IN ({in_clause})",
                vals,
            )
            updated = cur.rowcount > 0

        if updated:
            self._sync_active_marker()
        return updated

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def recover_orphaned_runs(self) -> list[str]:
        """Mark in-flight runs left over from a previous MCP server crash/restart as ESCALATED."""
        now = datetime.now(timezone.utc).isoformat()
        in_clause = ", ".join("?" for _ in IN_FLIGHT_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT run_id FROM runs WHERE status IN ({in_clause})",
                list(IN_FLIGHT_STATUSES),
            ).fetchall()
            orphaned_ids = [str(r["run_id"]) for r in rows]
            if orphaned_ids:
                conn.execute(
                    f"""
                    UPDATE runs
                    SET status = 'ESCALATED',
                        last_error_log = COALESCE(last_error_log, '') || ?,
                        updated_at = ?
                    WHERE status IN ({in_clause})
                    """,
                    [
                        "\n[系统恢复] MCP 服务重启导致后台任务中断，请调用 flow_resume 恢复或 flow_abort 退出。",
                        now,
                        *IN_FLIGHT_STATUSES,
                    ],
                )
        if orphaned_ids:
            self._sync_active_marker()
        return orphaned_ids

    def _sync_active_marker(self) -> None:
        """Keep .adk/active_run.json accurately synchronized with active DB runs."""
        in_clause = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT run_id, status FROM runs WHERE status IN ({in_clause}) ORDER BY updated_at DESC",
                list(ACTIVE_STATUSES),
            ).fetchall()
        if not rows:
            self.clear_active_run()
            return
        latest = rows[0]
        payload = {
            "run_id": latest["run_id"],
            "status": latest["status"],
            "active_run_ids": [r["run_id"] for r in rows],
            "active_runs": [{"run_id": r["run_id"], "status": r["status"]} for r in rows],
        }
        tmp_marker = self.active_marker.with_suffix(".tmp")
        tmp_marker.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_marker.replace(self.active_marker)

    def get_active_runs(self) -> list[dict[str, Any]]:
        in_clause = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({in_clause}) ORDER BY updated_at DESC",
                list(ACTIVE_STATUSES),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_run_active(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        return bool(run and run.get("status") in ACTIVE_STATUSES)

    def read_active_marker(self) -> dict[str, Any] | None:
        """Safely read and parse the active marker file (.adk/active_run.json), if present."""
        if not self.active_marker.is_file():
            return None
        try:
            data = json.loads(self.active_marker.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def clear_active_run(self) -> None:
        if self.active_marker.exists():
            try:
                self.active_marker.unlink()
            except OSError:
                pass
