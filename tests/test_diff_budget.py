from __future__ import annotations

import os
import unittest

from adk_dev_flow.workflow import (
    assemble_diff_blocks,
    get_max_diff_chars,
)


class TestDiffBudget(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = os.environ.get("ADK_MAX_DIFF_CHARS")

    def tearDown(self) -> None:
        if self._orig_env is None:
            os.environ.pop("ADK_MAX_DIFF_CHARS", None)
        else:
            os.environ["ADK_MAX_DIFF_CHARS"] = self._orig_env

    def test_dynamic_env_lookup_at_call_time(self) -> None:
        """ADK_MAX_DIFF_CHARS must be read at call time, not fixed at module import."""
        os.environ["ADK_MAX_DIFF_CHARS"] = "35000"
        self.assertEqual(get_max_diff_chars(), 35000)

        os.environ["ADK_MAX_DIFF_CHARS"] = "4800"
        self.assertEqual(get_max_diff_chars(), 4800)

        os.environ.pop("ADK_MAX_DIFF_CHARS", None)
        self.assertEqual(get_max_diff_chars(), 12000)

        # Invalid env falls back to 12000
        os.environ["ADK_MAX_DIFF_CHARS"] = "invalid_int"
        self.assertEqual(get_max_diff_chars(), 12000)

    def test_per_file_budget_prevents_test_file_starvation(self) -> None:
        """When an implementation file (e.g. docx_export.py) is large, subsequent test files

        must NOT be starved or dropped entirely. Each file gets fair budget share.
        """
        # docx_export.py is 11,500 chars (almost eats whole 12k budget in greedy mode)
        large_impl_diff = "diff --git a/src/docx_export.py\n" + ("+def export_block(): pass\n" * 450)
        test_file_diff = (
            "diff --git a/tests/test_docx_export.py\n"
            "+def test_export():\n"
            "+    assert docx_export() is True\n"
        )
        spec_diff = (
            "diff --git a/spec.md\n"
            "+# Spec Contract: docx export feature\n"
        )

        file_diffs = [
            ("src/docx_export.py", large_impl_diff),
            ("tests/test_docx_export.py", test_file_diff),
            ("spec.md", spec_diff),
        ]

        diff_text, is_truncated, truncated_files = assemble_diff_blocks(file_diffs, max_chars=12000)

        # Critic MUST see the test file and the spec in diff_text!
        self.assertIn("tests/test_docx_export.py", diff_text)
        self.assertIn("def test_export():", diff_text)
        self.assertIn("spec.md", diff_text)

        # The large file should be partially included with a truncation marker, not dropped
        self.assertIn("src/docx_export.py", diff_text)
        self.assertTrue(is_truncated)
        self.assertIn("src/docx_export.py", truncated_files)
        self.assertIn("[DIFF TRUNCATED FOR src/docx_export.py", diff_text)

        # Total diff_text length must respect the overall max_chars budget (with small banner overhead)
        self.assertLessEqual(len(diff_text), 12500)

    def test_within_budget_files_are_not_truncated(self) -> None:
        """When total diff fits within max_chars, no files are truncated."""
        file_diffs = [
            ("src/service.py", "diff --git a/src/service.py\n+def foo(): return 1\n"),
            ("tests/test_service.py", "diff --git a/tests/test_service.py\n+def test_foo(): assert foo() == 1\n"),
        ]
        diff_text, is_truncated, truncated_files = assemble_diff_blocks(file_diffs, max_chars=12000)
        self.assertFalse(is_truncated)
        self.assertEqual(truncated_files, [])
        self.assertIn("def foo():", diff_text)
        self.assertIn("def test_foo():", diff_text)


if __name__ == "__main__":
    unittest.main()
