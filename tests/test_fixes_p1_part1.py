from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.models import AtomicTask, ReviewVerdict, SpecContract
from adk_dev_flow.workflow import _format_mr_description
from adk_dev_flow.worktree_utils import resolve_tool_command


class TestFixesP1Part1(unittest.TestCase):
    def test_p1_5_shlex_split_preserves_windows_paths_with_spaces_and_backslashes(self) -> None:
        """P1-5: On Windows, shlex.split with posix=False must preserve backslashes and quoted paths with spaces."""
        override = r'C:\tools\new\python.exe -m pytest "D:\My Tests\test_x.py"'
        cmd = resolve_tool_command(
            "pytest",
            ["-q"],
            worktree=Path("."),
            repo_root=Path("."),
            env_override=override,
        )
        self.assertIsNotNone(cmd)
        if sys.platform == "win32":
            self.assertEqual(cmd[0], r"C:\tools\new\python.exe")
            self.assertEqual(cmd[1], "-m")
            self.assertEqual(cmd[2], "pytest")
            self.assertEqual(cmd[3], r"D:\My Tests\test_x.py")

    def test_p1_1_mr_description_contains_ac_impact_and_verification(self) -> None:
        """P1-1: MR description must include Acceptance Criteria, Design impact, and Verification."""
        spec = SpecContract(
            title="Implement User Auth",
            summary="Add token verification",
            allowed_paths=["src/auth/"],
            acceptance_test_path="tests/test_auth.py",
            acceptance_test_code="def test_auth(): pass\n",
            require_tdd_red=True,
            extra_dependencies=["pyjwt"],
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Token parser",
                    description="Parse JWT header",
                    target_files=["src/auth/token.py"],
                )
            ],
        )
        verdict = ReviewVerdict(
            approved=True,
            confidence=0.95,
            summary="All ACs verified and no regression.",
        )
        desc = _format_mr_description(spec, verdict)

        # Must include AC, Design impact, and Verification
        self.assertIn("验收准则", desc)
        self.assertIn("tests/test_auth.py", desc)
        self.assertIn("T1: Token parser", desc)
        self.assertIn("影响面", desc)
        self.assertIn("src/auth", desc)
        self.assertIn("pyjwt", desc)
        self.assertIn("Step 8 确定性静态门禁", desc)


if __name__ == "__main__":
    unittest.main()
