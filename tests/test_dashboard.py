from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from adk_dev_flow.dashboard import create_dashboard_app, get_dashboard_html
from adk_dev_flow.mcp_server import create_mcp_server
from adk_dev_flow.state_store import StateStore


class TestDashboardSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp.name).resolve()
        self.store = StateStore(self.repo_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_get_dashboard_html_structure_and_injection(self) -> None:
        html = get_dashboard_html()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("ADK 像素工坊", html)
        self.assertIn("node-spec", html)
        self.assertIn("node-work", html)
        self.assertIn("node-test", html)
        self.assertIn("node-critic", html)
        self.assertIn("node-mr", html)
        self.assertIn("node-escalate", html)
        self.assertIn("topology-svg", html)
        self.assertIn("animate-hammer", html)
        self.assertIn("row-spec-design", html)
        self.assertIn("spec-red-evidence", html)
        self.assertIn("spec-pre-critic", html)
        self.assertIn("spec-impact", html)
        self.assertIn("step-pill-1", html)
        self.assertIn("step-pill-8", html)
        self.assertIn("active-step-badge", html)
        self.assertIn("setStep", html)

        # Test run_id injection
        injected = get_dashboard_html(initial_run_id="run_custom_999")
        self.assertIn('let currentRunId = "run_custom_999";', injected)

    def test_dashboard_app_endpoints(self) -> None:
        app = create_dashboard_app(repo_root=self.repo_root, store=self.store)
        client = TestClient(app)

        # 1. Index route
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("ADK 像素工坊", resp.text)

        # 2. Status route without any runs
        resp_status = client.get("/api/status")
        self.assertEqual(resp_status.status_code, 200)
        self.assertEqual(resp_status.json()["status"], "IDLE")

        # 3. Create a run and test status
        self.store.create_run("run_abc_1", issue_iid=42)
        self.store.update_run(
            "run_abc_1",
            status="RUNNING",
            branch="feat/test-branch",
            attempt=1,
            last_error_log="[Worker] 🔨 正在编辑代码",
            spec_json='{"title": "Demo", "design_doc": "# Architecture Design", "red_evidence": {"verdict": "PASSED_RED", "exit_code": 1}, "pre_critic_report": {"approved": true}, "impact_analysis": {"impacted_callers": ["foo()"]}}',
        )

        resp_status2 = client.get("/api/status?run_id=run_abc_1")
        self.assertEqual(resp_status2.status_code, 200)
        data = resp_status2.json()
        self.assertEqual(data["run_id"], "run_abc_1")
        self.assertEqual(data["status"], "RUNNING")
        self.assertEqual(data["attempt"], 1)
        self.assertIn("正在编辑代码", data["last_log"])
        self.assertEqual(data["design_doc"], "# Architecture Design")
        self.assertEqual(data["red_evidence"]["verdict"], "PASSED_RED")
        self.assertTrue(data["pre_critic_report"]["approved"])
        self.assertEqual(data["impact_analysis"]["impacted_callers"], ["foo()"])

        # 4. List runs route
        resp_runs = client.get("/api/runs")
        self.assertEqual(resp_runs.status_code, 200)
        runs_list = resp_runs.json()["runs"]
        self.assertEqual(len(runs_list), 1)
        self.assertEqual(runs_list[0]["run_id"], "run_abc_1")

    def test_flow_dashboard_mcp_integration(self) -> None:
        mcp, service = create_mcp_server(repo_root=self.repo_root, store=self.store)

        # Before any runs
        res_empty = service.flow_dashboard()
        self.assertIn("ADK 像素工坊看板已就绪", res_empty["content"])
        self.assertEqual(res_empty["structuredContent"]["status"], "IDLE")

        # After creating and updating a run
        self.store.create_run("run_mcp_demo")
        self.store.update_run(
            "run_mcp_demo",
            status="VERIFYING",
            attempt=2,
            last_error_log="[Tester] 🧪 pytest passed",
        )

        res_active = service.flow_dashboard("run_mcp_demo")
        self.assertEqual(res_active["structuredContent"]["run_id"], "run_mcp_demo")
        self.assertEqual(res_active["structuredContent"]["status"], "VERIFYING")
        self.assertEqual(res_active["structuredContent"]["attempt"], 2)


if __name__ == "__main__":
    unittest.main()
