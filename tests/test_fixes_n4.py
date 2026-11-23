from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.mcp_server import create_mcp_server
from adk_dev_flow.state_store import StateStore


class TestFixesN4(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir(parents=True)
        self.store = StateStore(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_n4_escalated_run_clears_active_marker(self) -> None:
        """N4: When a run transitions to ESCALATED, active_run.json marker must be cleared to allow self-healing."""
        self.store.create_run("run-crash-1")
        self.store.update_run("run-crash-1", status="RUNNING")
        self.assertTrue(self.store.active_marker.exists())

        # Transition to ESCALATED
        self.store.update_run("run-crash-1", status="ESCALATED")
        self.assertFalse(
            self.store.active_marker.exists(),
            "Marker must be cleared when run is ESCALATED so workspace is not locked read-only.",
        )

    def test_n4_orphan_recovery_clears_active_marker(self) -> None:
        """N4: Recovering orphaned runs after MCP crash must clear active marker."""
        self.store.create_run("run-in-flight-1")
        self.store.update_run("run-in-flight-1", status="RUNNING")
        self.assertTrue(self.store.active_marker.exists())

        orphaned = self.store.recover_orphaned_runs()
        self.assertIn("run-in-flight-1", orphaned)
        self.assertFalse(
            self.store.active_marker.exists(),
            "After orphan recovery, active marker must be cleared.",
        )

    async def test_n4_flow_unlock_tool_and_status_hint(self) -> None:
        """N4: flow_unlock MCP tool and flow_status explicit marker guidance."""
        _, service = create_mcp_server(repo_root=self.root, store=self.store)
        await service.flow_init("Req")
        self.assertTrue(self.store.active_marker.exists())

        # flow_unlock clears marker
        unlock_res = service.flow_unlock()
        self.assertTrue(unlock_res["unlocked"])
        self.assertFalse(self.store.active_marker.exists())


if __name__ == "__main__":
    unittest.main()
