from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class AtomicTask(BaseModel):
    id: str = Field(description="任务唯一ID，如 T1")
    title: str
    description: str
    target_files: list[str] = Field(description="允许修改的相对路径列表")


class SpecContract(BaseModel):
    issue_iid: int | None = None
    title: str
    summary: str
    target_branch: str = "main"
    allowed_paths: list[str] = Field(description="允许修改的目录或文件前缀白名单")
    acceptance_test_path: str = Field(
        description="锁定的验收测试文件相对路径，如 tests/test_issue_42.py"
    )
    acceptance_test_code: str = Field(description="完整可运行的 pytest 验收测试源码")
    tasks: list[AtomicTask]


class ReviewVerdict(BaseModel):
    approved: bool
    blocking_issues: list[str] = Field(default_factory=list)
    summary: str


class StateStore:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.adk_dir = repo_root / ".adk"
        self.adk_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.adk_dir / "workflow.db"
        self.active_marker = self.adk_dir / "active_run.json"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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

    def create_run(self, run_id: str, issue_iid: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, status, issue_iid, updated_at) VALUES (?, ?, ?, ?)",
                (run_id, "SPEC_DRAFTING", issue_iid, now),
            )
        self.set_active_run(run_id, "SPEC_DRAFTING")

    def update_run(self, run_id: str, **fields: Any) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [run_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", vals)
        if "status" in fields:
            if fields["status"] in ("MR_READY", "ABORTED"):
                self.clear_active_run()
            else:
                self.set_active_run(run_id, fields["status"])

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def set_active_run(self, run_id: str, status: str) -> None:
        self.active_marker.write_text(
            json.dumps({"run_id": run_id, "status": status}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear_active_run(self) -> None:
        if self.active_marker.exists():
            self.active_marker.unlink()
