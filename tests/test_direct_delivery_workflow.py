from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from adk_dev_flow.forge_client import BaseGitForge
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore, is_valid_transition, lifecycle_highlight_critic, lifecycle_step
from adk_dev_flow.workflow import build_dev_workflow


class DummyForge(BaseGitForge):
    @property
    def platform_name(self) -> str:
        return "dummy"

    @property
    def enabled(self) -> bool:
        return False

    def get_issue(self, issue_iid: int) -> dict:
        return {"iid": issue_iid, "title": "Test", "description": "Desc"}

    def create_or_update_mr(self, **kwargs) -> dict:
        return {"iid": 1, "web_url": "https://example.com/mr/1"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        pass

    def mark_mr_ready(self, mr_iid: int) -> dict:
        return {"iid": mr_iid, "ready": True}


class TestDirectDeliveryWorkflow(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / ".git").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        self.store = StateStore(self.repo / "state.db")
        self.forge = DummyForge()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_verifying_to_mr_ready_direct_transition_allowed_in_state_store(self) -> None:
        """Requirement: Direct transition from VERIFYING to MR_READY must be mathematically admissible."""
        self.assertTrue(
            is_valid_transition("VERIFYING", "MR_READY"),
            "VALID_TRANSITIONS must permit direct delivery: VERIFYING -> MR_READY",
        )

        run_id = "test-direct-trans"
        self.store.create_run(run_id)
        self.store.update_run(run_id, status="VERIFYING")

        ok = self.store.transition_run(
            run_id,
            expected_statuses=["VERIFYING"],
            new_status="MR_READY",
            enforce_matrix=True,
        )
        self.assertTrue(ok, "transition_run(VERIFYING -> MR_READY, enforce_matrix=True) must succeed")

    def test_critic_highlight_belongs_to_pre_lock_step_3(self) -> None:
        """Critic is highlighted during Pre-Lock Step 3 (PRE_CRITIC_REVIEWED) where contracts are vetted."""
        self.assertTrue(lifecycle_highlight_critic("PRE_CRITIC_REVIEWED"))

    async def test_workflow_direct_delivery_reaches_mr_ready_without_critic_call(self) -> None:
        """Requirement: When tests pass, workflow directly validates AST and delivers to MR_READY

        without requiring an intermediate blocking LLM Critic agent.
        """
        run_id = "direct-run-001"
        self.store.create_run(run_id, issue_iid=101)

        spec = SpecContract(
            issue_iid=101,
            title="Direct Physical Delivery",
            summary="Direct test",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_direct.py",
            acceptance_test_code="def test_d(): assert True\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task 1",
                    description="Desc",
                    target_files=["src/service.py"],
                )
            ],
        )
        self.store.update_run(
            run_id,
            status="VERIFYING",
            spec_json=spec.model_dump_json(),
            worktree_path=str(self.repo),
            branch="feat/direct-1",
        )

        # Create dummy file in repo to simulate clean implementation
        svc_file = self.repo / "src" / "service.py"
        svc_file.write_text("def process_data(payload: dict) -> bool:\n    return True\n", encoding="utf-8")

        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)

        # Find publish_mr_node / delivery node
        publish_node = None
        for edge in wf.edges:
            for item in edge:
                if callable(item) and item.__name__ in ("publish_mr_node", "deliver_mr_node"):
                    publish_node = item
                elif isinstance(item, (list, tuple)):
                    for sub in item:
                        if callable(sub) and sub.__name__ in ("publish_mr_node", "deliver_mr_node"):
                            publish_node = sub
                elif isinstance(item, dict):
                    for sub in item.values():
                        if callable(sub) and sub.__name__ in ("publish_mr_node", "deliver_mr_node"):
                            publish_node = sub

        self.assertIsNotNone(publish_node, "Delivery node must exist in workflow")

        # Verify edge routing: verify_python_node must directly route "passed" to delivery node
        verify_node = None
        for edge in wf.edges:
            if callable(edge[0]) and edge[0].__name__ == "verify_python_node":
                routes = edge[1]
                self.assertIsInstance(routes, dict)
                passed_target = routes.get("passed")
                self.assertIsNotNone(passed_target)
                self.assertEqual(
                    passed_target.__name__,
                    publish_node.__name__,
                    "verify_python_node 'passed' must route directly to delivery node without blocking critic",
                )

        # Call delivery node directly with input from verify_node
        input_data = {
            "worktree": str(self.repo),
            "spec": spec.model_dump(),
            "attempt": 0,
        }
        res = await publish_node(input_data)
        self.assertEqual(res.get("status"), "MR_READY")

        run_record = self.store.get_run(run_id)
        self.assertIsNotNone(run_record)
        self.assertEqual(run_record["status"], "MR_READY")

    async def test_delivery_fails_closed_on_ast_violation(self) -> None:
        """Requirement: If AST gate detects breaking changes or dangerous calls, delivery escalates."""
        run_id = "direct-run-002"
        self.store.create_run(run_id, issue_iid=102)

        spec = SpecContract(
            issue_iid=102,
            title="Dangerous AST Delivery",
            summary="Attack test",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_direct.py",
            acceptance_test_code="def test_d(): assert True\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task 1",
                    description="Desc",
                    target_files=["src/service.py"],
                )
            ],
        )
        self.store.update_run(
            run_id,
            status="VERIFYING",
            spec_json=spec.model_dump_json(),
            worktree_path=str(self.repo),
            branch="feat/direct-2",
        )

        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        publish_node = None
        for edge in wf.edges:
            for item in edge:
                if callable(item) and item.__name__ in ("publish_mr_node", "deliver_mr_node"):
                    publish_node = item
                elif isinstance(item, dict):
                    for sub in item.values():
                        if callable(sub) and sub.__name__ in ("publish_mr_node", "deliver_mr_node"):
                            publish_node = sub

        # Simulate AST finding with dangerous call and breaking change
        fake_ast_finding = {
            "file_path": "src/service.py",
            "syntax_ok": True,
            "breaking_issues": ["符号 `process_data` 移除了参数 `payload`"],
            "dangerous_nodes": ["发现危险函数调用 `eval` (line 12)"],
            "has_issues": True,
        }

        input_data = {
            "worktree": str(self.repo),
            "spec": spec.model_dump(),
            "attempt": 0,
            "ast_findings": [fake_ast_finding],
        }
        res = await publish_node(input_data)
        self.assertEqual(res.get("status"), "ESCALATED")

        run_record = self.store.get_run(run_id)
        self.assertIsNotNone(run_record)
        self.assertEqual(run_record["status"], "ESCALATED")


if __name__ == "__main__":
    unittest.main()
