from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from adk_dev_flow import dashboard
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
        self.assertIn("row-spec-mutation", html)
        self.assertIn("gate-mutation-pill", html)
        self.assertIn("spec-impact", html)
        self.assertIn("step-pill-1", html)
        self.assertIn("step-pill-8", html)
        self.assertIn("active-step-badge", html)
        self.assertIn("setStep", html)
        self.assertIn("live-stage-banner", html)
        self.assertIn("banner-stage-pill", html)
        self.assertIn("banner-actor-pill", html)
        self.assertIn("banner-action-text", html)
        self.assertIn("banner-waiting-for", html)
        self.assertIn("banner-station-pos", html)
        self.assertNotIn("svg-critic-beam", html)
        self.assertNotIn("animate-scan", html)

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
        idle_data = resp_status.json()
        self.assertEqual(idle_data["status"], "IDLE")
        self.assertEqual(idle_data["active_actor"], "系统调度器")
        self.assertIn("node_info", idle_data)
        self.assertEqual(idle_data["node_info"]["actor_role"], "系统调度器")

        # 3. Create a run and test status
        self.store.create_run("run_abc_1", issue_iid=42)
        self.store.update_run(
            "run_abc_1",
            status="RUNNING",
            branch="feat/test-branch",
            attempt=1,
            last_error_log="[Worker] 🔨 正在编辑代码",
            spec_json='{"title": "Demo", "design_doc": "# Architecture Design", "red_evidence": {"verdict": "PASSED_RED", "exit_code": 1}, "pre_critic_report": {"approved": true}, "impact_analysis": {"impacted_callers": ["foo()"]}, "mutation_report": {"killed_mutants": 10, "total_mutants": 10, "kill_ratio": 1.0, "passed": true}}',
        )

        resp_status2 = client.get("/api/status?run_id=run_abc_1")
        self.assertEqual(resp_status2.status_code, 200)
        data = resp_status2.json()
        self.assertEqual(data["run_id"], "run_abc_1")
        self.assertEqual(data["status"], "RUNNING")
        self.assertEqual(data["step"], 5)
        self.assertFalse(data["highlight_critic"])
        self.assertEqual(data["attempt"], 1)
        self.assertEqual(data["active_actor"], "开发工兵 Agent (Worker Agent)")
        self.assertIn("开发工兵 Agent", data["current_action"])
        self.assertIn("等待工兵编码完成提交", data["waiting_for"])
        self.assertEqual(data["node_info"]["station_id"], 5)
        self.assertIn("正在编辑代码", data["last_log"])
        self.assertEqual(data["design_doc"], "# Architecture Design")
        self.assertEqual(data["red_evidence"]["verdict"], "PASSED_RED")
        self.assertTrue(data["pre_critic_report"]["approved"])
        self.assertEqual(data["impact_analysis"]["impacted_callers"], ["foo()"])
        self.assertEqual(data["mutation_report"]["kill_ratio"], 1.0)
        self.assertTrue(data["require_mutation_gate"])

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

    def _init_git_repo(self) -> None:
        """Turn the tmp dir into a real git repo with one commit on main."""
        import subprocess as sp

        def g(*args: str) -> None:
            sp.run(["git", *args], cwd=str(self.repo_root), check=True, capture_output=True)

        g("init")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "t")
        (self.repo_root / "f.txt").write_text("x", encoding="utf-8")
        g("add", ".")
        g("commit", "-m", "init")
        g("branch", "-m", "main")

    def test_outside_activity_not_masked_by_zombie_runs(self) -> None:
        """Regression: zombie SPEC_DRAFTING runs must not hide unregistered branch work."""
        import subprocess as sp

        self._init_git_repo()
        sp.run(
            ["git", "checkout", "-b", "feat/rogue"],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
        )
        (self.repo_root / "f.txt").write_text("y", encoding="utf-8")
        sp.run(["git", "commit", "-am", "rogue work"], cwd=str(self.repo_root), check=True, capture_output=True)

        # Zombie run: SPEC_DRAFTING forever, claims no branch.
        self.store.create_run("run_zombie_1", issue_iid=71)

        app = create_dashboard_app(repo_root=self.repo_root, store=self.store)
        client = TestClient(app)
        data = client.get("/api/status").json()
        oa = data["outside_activity"]
        self.assertIsNotNone(oa)
        self.assertEqual(oa["branch"], "feat/rogue")
        self.assertIn("flow_adopt", oa["hint"])

        # Once a run claims the branch, the banner disappears.
        self.store.create_run("run_claim_1", issue_iid=72)
        self.store.update_run("run_claim_1", status="RUNNING", branch="feat/rogue")
        data = client.get("/api/status").json()
        self.assertIsNone(data["outside_activity"])

    def test_outside_activity_silent_on_main_branch(self) -> None:
        """main is the landing zone: compliant merges from MR_READY runs land there."""
        self._init_git_repo()  # HEAD stays on main
        self.store.create_run("run_zombie_2", issue_iid=70)

        app = create_dashboard_app(repo_root=self.repo_root, store=self.store)
        client = TestClient(app)
        data = client.get("/api/status").json()
        self.assertIsNone(data["outside_activity"])

    def test_stale_minutes_computed_for_active_runs(self) -> None:
        """Payload carries stale_minutes so clients can flag stalled runs honestly."""
        self.store.create_run("run_stale_1", issue_iid=73)

        app = create_dashboard_app(repo_root=self.repo_root, store=self.store)
        client = TestClient(app)
        data = client.get("/api/status?run_id=run_stale_1").json()
        self.assertIsInstance(data["stale_minutes"], int)
        self.assertGreaterEqual(data["stale_minutes"], 0)

    def test_dashboard_detach_spawns_server_without_blocking(self) -> None:
        with mock.patch.object(dashboard.subprocess, "Popen") as popen:
            code = dashboard.main(["--detach", "--port", "8099", "--repo", str(self.repo_root)])

        self.assertEqual(code, 0)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[1:3], ["-m", "adk_dev_flow.dashboard"])
        self.assertEqual(cmd[cmd.index("--port") + 1], "8099")
        self.assertEqual(Path(cmd[cmd.index("--repo") + 1]), self.repo_root)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
