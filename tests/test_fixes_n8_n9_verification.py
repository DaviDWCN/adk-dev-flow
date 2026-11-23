from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.cli import init_target_repo
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, ReviewVerdict, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import _format_mr_description, _validate_tdd_red_outcome


class TestFixesN8N9AndVerification(unittest.TestCase):
    def test_n8_flow_status_reports_lock_correctly(self) -> None:
        """N8: flow_status must accurately report is_holding_lock and current_locked_run_id."""
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            store = StateStore(repo_root)
            service = FlowService(repo_root, store)
            run_id = "test-n8-run"
            service.store.create_run(run_id)

            status = service.flow_status(run_id)
            self.assertTrue(
                status.get("is_holding_lock"),
                f"Expected is_holding_lock=True for active run {run_id}, got {status}",
            )
            self.assertEqual(
                status.get("current_locked_run_id"),
                run_id,
                f"Expected current_locked_run_id={run_id}, got {status}",
            )

            # Check that active_run.json contains active_run_id key as well
            marker = service.store.read_active_marker()
            self.assertIsNotNone(marker)
            assert marker is not None
            self.assertEqual(marker.get("run_id"), run_id)
            self.assertEqual(marker.get("active_run_id"), run_id)

    def test_n9_gitignore_formatting_is_clean(self) -> None:
        """N9: init_target_repo must not produce redundant headers or blank lines in .gitignore."""
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            init_target_repo(repo_root)

            gitignore = repo_root / ".gitignore"
            self.assertTrue(gitignore.exists())
            content = gitignore.read_text(encoding="utf-8")

            # Must contain only ONE "# ADK dev flow state" header
            self.assertEqual(
                content.count("# ADK dev flow state"),
                1,
                f"Expected exactly 1 comment header, got:\n{content}",
            )
            # Must not contain consecutive empty lines
            self.assertNotIn("\n\n\n", content)
            # Must contain all required ignore patterns
            for pat in (".adk/state.db*", ".adk/active_run.json", ".adk/worktrees/", ".adk/locks/"):
                self.assertIn(pat, content)

    def test_tdd_red_rejects_fixture_not_found(self) -> None:
        """TDD preflight must reject test misconfigurations like fixture '...' not found."""
        combined_output = (
            "test_feature.py:10: in test_foo\n"
            "    def test_foo(missing_fixture):\n"
            "E       fixture 'missing_fixture' not found\n"
        )
        with self.assertRaises(RuntimeError) as cm:
            _validate_tdd_red_outcome(returncode=1, combined_output=combined_output)
        self.assertIn("fixture", str(cm.exception).lower())

    def test_mr_description_includes_performed_verification(self) -> None:
        """MR description must render 'Verification Performed' section with test commands & stats."""
        spec = SpecContract(
            title="Add Payment Handler",
            summary="Payment flow support",
            allowed_paths=["src/pay.py"],
            acceptance_test_path="tests/test_pay.py",
            acceptance_test_code="def test_payment_success(): assert True\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Payment",
                    description="handler",
                    target_files=["src/pay.py"],
                )
            ],
        )
        verdict = ReviewVerdict(
            approved=True,
            confidence=0.95,
            summary="Implementation adheres to spec.",
            blocking_issues=[],
        )
        verification = {
            "test_cmd": "pytest -q tests/test_pay.py",
            "test_exit": 0,
            "test_summary": "5 passed in 0.23s",
            "lint_cmd": "ruff check .",
            "lint_exit": 0,
            "audit_passed": True,
        }

        desc = _format_mr_description(spec, verdict, verification=verification)
        self.assertIn("Verification Performed", desc)
        self.assertIn("pytest -q tests/test_pay.py", desc)
        self.assertIn("5 passed in 0.23s", desc)
        self.assertIn("ruff check .", desc)
        self.assertIn("Worktree", desc)


if __name__ == "__main__":
    unittest.main()
