from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from adk_dev_flow.guard import evaluate_file_edit
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.subproc import CommandResult


class TestFixesP0(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name).resolve()
        (self.cwd / "src").mkdir(parents=True)
        (self.cwd / "tests").mkdir(parents=True)
        (self.cwd / ".adk" / "specs").mkdir(parents=True)
        (self.cwd / ".adk" / "knowledge" / "rules").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_n1_guard_allows_specs_and_knowledge_during_drafting(self) -> None:
        """N1: In SPEC_DRAFTING, .adk/specs and .adk/knowledge must be editable."""
        marker = self.cwd / ".adk" / "active_run.json"
        marker.write_text(
            json.dumps({"active_run_id": "run-draft-1", "status": "SPEC_DRAFTING"}),
            encoding="utf-8",
        )
        spec_path = ".adk/specs/issue-33.md"
        allowed, reason = evaluate_file_edit(spec_path, cwd=self.cwd, env={})
        self.assertTrue(allowed, f"Expected {spec_path} to be allowed during drafting, got: {reason}")

        rules_path = ".adk/knowledge/rules/engineering_rules.md"
        allowed, reason = evaluate_file_edit(rules_path, cwd=self.cwd, env={})
        self.assertTrue(allowed, f"Expected {rules_path} to be allowed during drafting, got: {reason}")

        # But protected internal state or source files in main repo must still be blocked
        allowed, reason = evaluate_file_edit(".adk/state.db", cwd=self.cwd, env={})
        self.assertFalse(allowed)
        allowed, reason = evaluate_file_edit("src/app.py", cwd=self.cwd, env={})
        self.assertFalse(allowed)

    def test_p0_3_guard_allows_configs_outside_active_flow(self) -> None:
        """P0-3: Outside active flow (no marker, no worker token), developer can edit configs."""
        configs = ["pyproject.toml", "conftest.py", ".gitignore", ".gitlab-ci.yml"]
        for cfg in configs:
            allowed, reason = evaluate_file_edit(cfg, cwd=self.cwd, env={})
            self.assertTrue(allowed, f"Expected {cfg} to be allowed outside flow, got: {reason}")

    @patch("adk_dev_flow.workflow.resolve_tool_command")
    async def test_p0_2_tdd_red_fails_closed_when_pytest_not_found(self, mock_resolve: AsyncMock) -> None:
        """P0-2: When pytest cannot be resolved, TDD Red must fail-closed with clear error."""
        from adk_dev_flow.workflow import run_tdd_red_preflight

        mock_resolve.return_value = None
        spec = SpecContract(
            title="Feature X",
            summary="Feature X summary",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_x.py",
            acceptance_test_code="def test_x(): assert False\n",
            require_tdd_red=True,
            tasks=[AtomicTask(id="T1", title="Task 1", description="desc", target_files=["src/x.py"])],
        )
        with self.assertRaises(RuntimeError) as ctx:
            await run_tdd_red_preflight(spec, worktree=self.cwd, repo_root=self.cwd)
        self.assertIn("pytest", str(ctx.exception))

    @patch("adk_dev_flow.workflow.run_subprocess_safe")
    @patch("adk_dev_flow.workflow.resolve_tool_command")
    async def test_p0_2_tdd_red_rejects_syntax_or_crash_error(
        self, mock_resolve: AsyncMock, mock_run: AsyncMock
    ) -> None:
        """P0-2: Exit code != 0 due to command/syntax crash (exit 2) must not count as assertion red light."""
        from adk_dev_flow.workflow import run_tdd_red_preflight

        mock_resolve.return_value = ["pytest", "-q", "tests/test_x.py"]
        # Exit code 2 (pytest command line error or test collection syntax error)
        mock_run.return_value = CommandResult(
            returncode=2,
            stdout="",
            stderr="ERROR: usage: pytest [options]\npytest: error: unrecognized arguments",
            timed_out=False,
        )
        spec = SpecContract(
            title="Feature X",
            summary="Feature X summary",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_x.py",
            acceptance_test_code="def test_x(): assert False\n",
            require_tdd_red=True,
            tasks=[AtomicTask(id="T1", title="Task 1", description="desc", target_files=["src/x.py"])],
        )
        with self.assertRaises(RuntimeError) as ctx:
            await run_tdd_red_preflight(spec, worktree=self.cwd, repo_root=self.cwd)
        self.assertIn("红灯预检未通过", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
