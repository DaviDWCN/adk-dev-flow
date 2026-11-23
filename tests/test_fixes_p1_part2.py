from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.codebuddy_bridge import validate_model_name
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.workflow import build_dev_workflow


class TestFixesP1Part2(unittest.TestCase):
    def test_p1_7_model_name_validation(self) -> None:
        """P1-7: Model names must be validated against dangerous characters and formats."""
        self.assertEqual(validate_model_name("gpt-4o"), "gpt-4o")
        self.assertEqual(validate_model_name("deepseek-chat"), "deepseek-chat")
        self.assertEqual(validate_model_name("claude-3-5-sonnet@20240620"), "claude-3-5-sonnet@20240620")
        self.assertEqual(validate_model_name("anthropic/claude-3.5"), "anthropic/claude-3.5")
        self.assertIsNone(validate_model_name("codebuddy-cli"))

        # Dangerous characters (injection attempts or syntax errors) must be rejected
        with self.assertRaises(ValueError):
            validate_model_name("model; rm -rf /")
        with self.assertRaises(ValueError):
            validate_model_name("model`whoami`")
        with self.assertRaises(ValueError):
            validate_model_name("model$(id)")

    def test_p1_2_custom_branch_naming_and_p1_3_stacked_branch(self) -> None:
        """P1-2: Custom branch pattern in environment or spec. P1-3: Stacked base_branch."""
        spec = SpecContract(
            title="Stacked Feature Part 2",
            summary="Stacked PR",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_s.py",
            acceptance_test_code="def test_s(): pass\n",
            require_tdd_red=False,
            target_branch="main",
            base_branch="feature/part-1",
            branch="feature/custom-part-2",
            tasks=[AtomicTask(id="T1", title="Task 1", description="desc", target_files=["src/s.py"])],
        )
        self.assertEqual(spec.base_branch, "feature/part-1")
        self.assertEqual(spec.branch, "feature/custom-part-2")
        self.assertEqual(spec.target_branch, "main")

    def test_n3_adk_python_override_support(self) -> None:
        """N3: ADK_PYTHON environment variable overrides default python executable."""
        from adk_dev_flow.worktree_utils import get_preferred_python_interpreter

        with patch.dict(os.environ, {"ADK_PYTHON": r"C:\CustomPython\python.exe"}, clear=False):
            py = get_preferred_python_interpreter()
            self.assertEqual(py, r"C:\CustomPython\python.exe")


if __name__ == "__main__":
    unittest.main()
