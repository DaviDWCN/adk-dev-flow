from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from adk_dev_flow.ast_graph import CodeGraph
from adk_dev_flow.mcp_server import create_mcp_server
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore


class TestFixesN7AndSpecBranch(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir(parents=True)
        self.store = StateStore(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_n7_suggested_allowed_paths_includes_all_impacted_and_tests(self) -> None:
        """N7: suggested_allowed_paths must contain all impacted production files (1-hop & 2-hop) and test files."""
        src_dir = self.root / "src"
        tests_dir = self.root / "tests"
        src_dir.mkdir(parents=True)
        tests_dir.mkdir(parents=True)

        (src_dir / "product_lines.py").write_text("def get_lines(): return []\n", encoding="utf-8")
        (src_dir / "release.py").write_text("from src.product_lines import get_lines\ndef do_release(): return get_lines()\n", encoding="utf-8")
        (src_dir / "vertical_slice.py").write_text("from src.release import do_release\ndef slice(): return do_release()\n", encoding="utf-8")
        (tests_dir / "test_product_lines.py").write_text("from src.product_lines import get_lines\ndef test_p(): pass\n", encoding="utf-8")

        graph = CodeGraph(self.root)
        graph.index_repo()

        report = graph.analyze_impact(["src/product_lines.py"])
        # All 3 production files and the test file must be in suggested_allowed_paths
        self.assertIn("src/product_lines.py", report.suggested_allowed_paths)
        self.assertIn("src/release.py", report.suggested_allowed_paths)
        self.assertIn("src/vertical_slice.py", report.suggested_allowed_paths)
        self.assertIn("tests/test_product_lines.py", report.suggested_allowed_paths)

    async def test_forge_warning_on_unconfigured_gitlab(self) -> None:
        """P0-5 fix verification: Passing issue_iid without configured forge must return non-silent warning."""
        fake_gl = MagicMock()
        fake_gl.enabled = False
        fake_gl.platform_name = "gitlab"

        _, service = create_mcp_server(repo_root=self.root, store=self.store, forge=fake_gl)
        res = await service.flow_init(requirement="Fix issue", issue_iid=31)

        self.assertIsNone(res.get("issue"))
        self.assertIsNotNone(res.get("forge_warning"), "Must return explicit warning when issue_iid is passed but forge is disabled.")
        self.assertIn("31", res["forge_warning"])
        self.assertIn("未检测到", res["forge_warning"])

    def test_guard_cli_hook_permits_spec_writing_during_drafting(self) -> None:
        """N1 verification: CLI hook python -m adk_dev_flow.guard must allow .adk/specs write in SPEC_DRAFTING."""
        marker = self.root / ".adk" / "active_run.json"
        marker.write_text(
            json.dumps({"run_id": "test-draft-4eaf", "status": "SPEC_DRAFTING"}),
            encoding="utf-8",
        )

        src_path = str(Path(__file__).resolve().parents[1] / "src")
        env = {
            **dict(os.environ),
            "PYTHONPATH": src_path,
        }

        # 1. Writing to .adk/specs/issue-31.md must be permitted (exit code 0)
        res_spec = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.guard"],
            input=json.dumps({"tool_input": {"file_path": ".adk/specs/issue-31.md"}}),
            text=True,
            capture_output=True,
            cwd=str(self.root),
            env=env,
        )
        self.assertEqual(res_spec.returncode, 0, f"Expected 0 for spec draft, got stderr: {res_spec.stderr}")

        # 2. Writing to .adk/knowledge/rules/engineering_rules.md must be permitted (exit code 0)
        res_rules = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.guard"],
            input=json.dumps({"tool_input": {"file_path": ".adk/knowledge/rules/engineering_rules.md"}}),
            text=True,
            capture_output=True,
            cwd=str(self.root),
            env=env,
        )
        self.assertEqual(res_rules.returncode, 0, f"Expected 0 for rules draft, got stderr: {res_rules.stderr}")

        # 3. Writing to business source src/app.py must be blocked during drafting (exit code 2)
        res_src = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.guard"],
            input=json.dumps({"tool_input": {"file_path": "src/app.py"}}),
            text=True,
            capture_output=True,
            cwd=str(self.root),
            env=env,
        )
        self.assertEqual(res_src.returncode, 2, "Expected 2 for source edit during drafting")

    async def test_workflow_commits_spec_contract_to_worktree(self) -> None:
        """Point 4: SpecContract must be saved to .adk/specs/ and committed to Git in worktree."""
        from adk_dev_flow.workflow import save_and_commit_spec_in_worktree

        wt = self.root / ".adk" / "worktrees" / "run-spec-test"
        wt.mkdir(parents=True, exist_ok=True)
        # Initialize git inside worktree
        subprocess.run(["git", "init"], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(wt), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(wt), check=True)
        (wt / "README.md").write_text("# Initial", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(wt), check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(wt), check=True)

        spec = SpecContract(
            issue_iid=31,
            title="Implement User Export",
            summary="Export users to CSV and JSON",
            allowed_paths=["src/export/"],
            acceptance_test_path="tests/test_export.py",
            acceptance_test_code="def test_export(): pass\n",
            require_tdd_red=False,
            tasks=[AtomicTask(id="T1", title="Export service", description="desc", target_files=["src/export/csv.py"])],
        )

        ok, spec_file = await save_and_commit_spec_in_worktree(spec, worktree=wt, run_id="run-spec-test")
        self.assertTrue(ok)
        self.assertTrue(spec_file.is_file())
        self.assertIn("Implement User Export", spec_file.read_text(encoding="utf-8"))

        # Verify git commit contains the spec file
        log_res = subprocess.run(["git", "log", "-n", "1", "--oneline"], cwd=str(wt), capture_output=True, text=True)
        self.assertIn("docs(spec)", log_res.stdout)


if __name__ == "__main__":
    unittest.main()
