from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.dashboard import get_dashboard_html
from adk_dev_flow.forge_client import BaseGitForge
from adk_dev_flow.mcp_server import FlowService, create_mcp_server
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import build_dev_workflow


class _DummyForge(BaseGitForge):
    @property
    def platform_name(self) -> str:
        return "dummy"

    @property
    def enabled(self) -> bool:
        return False

    def get_issue(self, issue_iid: int) -> dict:
        return {"iid": issue_iid, "title": "Dummy", "description": "Dummy"}

    def create_or_update_mr(self, **kwargs) -> dict:
        return {"iid": 1, "web_url": "https://example.com/mr/1"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        pass

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


class TestTask3StateAndCriticFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "pkg" / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

        self.store = StateStore(self.repo)
        self.forge = _DummyForge()
        self.service = FlowService(repo_root=self.repo, store=self.store, forge=self.forge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_prelock_intermediate_states_and_enforce_matrix(self) -> None:
        run_id = "run-state-01"
        self.store.create_run(run_id, issue_iid=11)
        recorded_transitions: list[tuple[str, bool]] = []
        orig_transition = self.store.transition_run

        def spy_transition(rid, *, expected_statuses, new_status, enforce_matrix=False, **fields):
            recorded_transitions.append((new_status, enforce_matrix))
            return orig_transition(
                rid,
                expected_statuses=expected_statuses,
                new_status=new_status,
                enforce_matrix=enforce_matrix,
                **fields,
            )

        spec = SpecContract(
            issue_iid=11,
            title="Add sub",
            summary="Add sub function",
            allowed_paths=["src/pkg/calc.py"],
            acceptance_test_path="tests/test_calc.py",
            acceptance_test_code="from pkg.calc import add\ndef test_a(): assert add(1, 2) == 999\n",
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Add sub", description="sub", target_files=["src/pkg/calc.py"])],
        )

        with (
            patch.dict(os.environ, {"ADK_WORKER_PROVIDER": "mock"}, clear=False),
            patch.object(self.store, "transition_run", side_effect=spy_transition),
        ):
            res = self.service.flow_analyze_impact(["src/pkg/calc.py"], run_id=run_id)
            self.assertEqual(self.store.get_run(run_id)["status"], "SPEC_DESIGNED")
            self.assertEqual(res.get("status"), "SPEC_DESIGNED")

            red_res = self.service.flow_preflight_test(
                spec.acceptance_test_path, spec.acceptance_test_code, run_id=run_id
            )
            self.assertTrue(red_res["ok"], red_res)
            self.assertEqual(self.store.get_run(run_id)["status"], "TEST_GENERATED")

            pc_res = await self.service.flow_pre_critic(run_id=run_id, spec=spec)
            self.assertTrue(pc_res["approved"], pc_res)
            self.assertEqual(self.store.get_run(run_id)["status"], "PRE_CRITIC_REVIEWED")

            lock_res = self.service.flow_lock_spec(run_id, spec)

        self.assertEqual(lock_res.get("status"), "SPEC_LOCKED")
        self.assertIn(("SPEC_DESIGNED", True), recorded_transitions)
        self.assertIn(("TEST_GENERATED", True), recorded_transitions)
        self.assertIn(("PRE_CRITIC_REVIEWED", True), recorded_transitions)
        self.assertIn(("SPEC_LOCKED", True), recorded_transitions)

    async def test_publish_mr_transitions_through_reviewing_deterministically_without_ai_critic(self) -> None:
        run_id = "run-critic-01"
        self.store.create_run(run_id, issue_iid=12)
        spec = SpecContract(
            issue_iid=12,
            title="Deterministic Step 8 Check",
            summary="Step 8 must be deterministic AST gate without invoking AI Critic",
            allowed_paths=["src/pkg/calc.py"],
            acceptance_test_path="tests/test_calc.py",
            acceptance_test_code="def test_ok(): assert True\n",
            tasks=[AtomicTask(id="T1", title="Edit calc", description="edit", target_files=["src/pkg/calc.py"])],
        )
        self.store.update_run(
            run_id,
            status="VERIFYING",
            spec_json=spec.model_dump_json(),
            worktree_path=str(self.repo),
            branch="main",
            base_sha="HEAD",
        )

        seen_statuses: list[tuple[str, bool]] = []
        orig_transition = self.store.transition_run

        def spy_transition(rid, *, expected_statuses, new_status, enforce_matrix=False, **fields):
            seen_statuses.append((new_status, enforce_matrix))
            return orig_transition(
                rid,
                expected_statuses=expected_statuses,
                new_status=new_status,
                enforce_matrix=enforce_matrix,
                **fields,
            )

        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        publish_node = _extract_node(wf, "publish_mr_node")
        self.assertIsNotNone(publish_node)

        async def fake_critic(*args, **kwargs):
            return {
                "approved": False,
                "confidence": 0.92,
                "summary": "Should not run in Step 8.",
                "blocking_issues": ["Should not block"],
            }

        with (
            patch.dict(os.environ, {"ADK_ENABLE_CRITIC": "1"}),
            patch("adk_dev_flow.workflow.run_readonly_critic_review", side_effect=fake_critic) as m_critic,
            patch.object(self.store, "transition_run", side_effect=spy_transition),
        ):
            res = await publish_node({"worktree": str(self.repo), "spec": spec.model_dump(), "attempt": 0})

        m_critic.assert_not_awaited()
        self.assertEqual(res.get("status"), "MR_READY")
        self.assertIn(("REVIEWING", True), seen_statuses)
        self.assertIn(("MR_READY", True), seen_statuses)
        self.assertEqual(self.store.get_run(run_id)["status"], "MR_READY")

    async def test_explicit_missing_base_branch_fails_without_head_fallback(self) -> None:
        run_id = "run-bad-base"
        self.store.create_run(run_id, issue_iid=13)
        spec = SpecContract(
            issue_iid=13,
            title="Stacked Missing Base",
            summary="Explicit base_branch must fail loudly if absent",
            base_branch="nonexistent/stacked-base",
            allowed_paths=["src/pkg/calc.py"],
            acceptance_test_path="tests/test_calc.py",
            acceptance_test_code="def test_x(): assert False\n",
            require_tdd_red=False,
            tasks=[AtomicTask(id="T1", title="Edit", description="edit", target_files=["src/pkg/calc.py"])],
        )
        self.store.update_run(run_id, status="SPEC_LOCKED", spec_json=spec.model_dump_json())
        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        prepare_node = _extract_node(wf, "prepare_worktree_node")
        with self.assertRaises(RuntimeError) as ctx:
            await prepare_node(json.dumps({"spec": spec.model_dump()}))
        self.assertIn("nonexistent/stacked-base", str(ctx.exception))


class TestTask6PreflightAndAsyncMcp(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.store = StateStore(self.repo)
        self.forge = _DummyForge()
        self.service = FlowService(repo_root=self.repo, store=self.store, forge=self.forge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_preflight_rejects_path_traversal_absolute_and_protected_paths(self) -> None:
        outside_path = self.repo.parent / "escaped_preflight.py"
        abs_path = str(self.repo / "tests" / "test_abs.py")
        for bad_path in ("../escaped_preflight.py", abs_path, ".adk/env.json", "pyproject.toml", ".git/config"):
            res = self.service.flow_preflight_test(bad_path, "def test_x(): assert False\n")
            self.assertFalse(res.get("ok"), f"Expected rejection for {bad_path}, got {res}")
            self.assertIn("error", res)
        self.assertFalse(outside_path.exists())
        self.assertFalse((self.repo / ".adk" / "env.json").exists())

    async def test_mcp_blocking_tools_are_async_and_do_not_block_event_loop(self) -> None:
        mcp, service = create_mcp_server(repo_root=self.repo, store=self.store, forge=self.forge)

        for tool_name in ("flow_lock_spec", "flow_lock_spec_from_file", "flow_analyze_impact", "flow_preflight_test"):
            tool_obj = await mcp.get_tool(tool_name)
            self.assertTrue(
                inspect.iscoroutinefunction(tool_obj.fn),
                f"MCP tool {tool_name} must be async to avoid blocking FastMCP event loop",
            )

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        def slow_preflight(*args, **kwargs):
            time.sleep(0.15)
            return {"ok": True, "exit_code": 1, "is_pure_semantic_failure": True}

        with patch.object(service, "flow_preflight_test", side_effect=slow_preflight):
            preflight_tool = await mcp.get_tool("flow_preflight_test")
            _, res = await asyncio.gather(
                ticker(),
                preflight_tool.fn(test_path="tests/test_x.py", test_code="def test_x(): assert False\n"),
            )
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(ticks, 3, "Concurrent coroutine ticks stalled on blocked event loop!")


class TestDashboardParity(unittest.TestCase):
    def test_dashboard_html_renders_adoption_outside_activity_and_merged_after_abort(self) -> None:
        html = get_dashboard_html()
        self.assertNotIn("svg-critic-beam", html)
        self.assertIn("outside_activity", html)
        self.assertIn("merged_after_abort", html)
        self.assertIn("post_hoc", html)


if __name__ == "__main__":
    unittest.main()
