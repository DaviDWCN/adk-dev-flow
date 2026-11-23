from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.guard import evaluate_file_edit, is_path_under, normalize_relative_path


class TestGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name).resolve()
        (self.cwd / "src" / "api").mkdir(parents=True)
        (self.cwd / "src" / "api_admin").mkdir(parents=True)
        (self.cwd / "tests").mkdir(parents=True)
        (self.cwd / ".adk").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_is_path_under_respects_directory_boundaries(self) -> None:
        self.assertTrue(is_path_under("src/api/routes.py", "src/api"))
        self.assertTrue(is_path_under("src/api/routes.py", "src/api/"))
        self.assertTrue(is_path_under("src/api", "src/api"))
        # Must NOT suffer prefix collision
        self.assertFalse(is_path_under("src/api_admin/secret.py", "src/api"))
        self.assertFalse(is_path_under("src/api_admin/secret.py", "src/api/"))

    def test_normalize_relative_path_handles_absolute_and_blocks_traversal(self) -> None:
        abs_inside = str(self.cwd / "src" / "api" / "handler.py")
        self.assertEqual(normalize_relative_path(abs_inside, self.cwd), "src/api/handler.py")
        self.assertEqual(
            normalize_relative_path("./src/api/../api/handler.py", self.cwd),
            "src/api/handler.py",
        )
        # Escaping workspace must return None
        self.assertIsNone(normalize_relative_path("../../etc/passwd", self.cwd))
        self.assertIsNone(normalize_relative_path("/etc/passwd", self.cwd))

    def test_protected_files_blocked_for_relative_absolute_and_traversal(self) -> None:
        env = {
            "ADK_WORKER_TOKEN": "run-1",
            "ADK_ALLOWED_PATHS": json.dumps(["src"]),
            "ADK_LOCKED_TESTS": json.dumps(["tests/test_x.py"]),
        }
        cases = [
            ".gitlab-ci.yml",
            str(self.cwd / ".gitlab-ci.yml"),
            "src/../.gitlab-ci.yml",
            ".codebuddy/settings.json",
            str(self.cwd / ".codebuddy" / "hooks" / "workflow_guard.py"),
            ".adk/active_run.json",
            "tools/adk_dev_flow/workflow.py",
            "mcp.json",
        ]
        for path in cases:
            allowed, reason = evaluate_file_edit(path, cwd=self.cwd, env=env)
            self.assertFalse(allowed, f"Expected protected path to be blocked: {path}")
            self.assertIn("受保护", reason)

    def test_worker_allowed_absolute_path_and_blocks_collision_and_locked_test(self) -> None:
        env = {
            "ADK_WORKER_TOKEN": "run-1",
            "ADK_ALLOWED_PATHS": json.dumps(["src/api"]),
            "ADK_LOCKED_TESTS": json.dumps(["tests/test_issue_1.py"]),
        }
        # Absolute path inside whitelist MUST be allowed (fixes v1 lstrip bug)
        ok, _ = evaluate_file_edit(str(self.cwd / "src" / "api" / "v1.py"), cwd=self.cwd, env=env)
        self.assertTrue(ok)

        # Prefix collision MUST be blocked
        ok, reason = evaluate_file_edit(
            str(self.cwd / "src" / "api_admin" / "v1.py"), cwd=self.cwd, env=env
        )
        self.assertFalse(ok)
        self.assertIn("白名单", reason)

        # Locked test (absolute or relative) MUST be blocked
        for locked_p in ["tests/test_issue_1.py", str(self.cwd / "tests" / "test_issue_1.py")]:
            ok, reason = evaluate_file_edit(locked_p, cwd=self.cwd, env=env)
            self.assertFalse(ok)
            self.assertIn("锁定的验收测试", reason)

    def test_commander_blocked_when_active_run_marker_exists(self) -> None:
        marker = self.cwd / ".adk" / "active_run.json"
        ok_before, _ = evaluate_file_edit("src/api/app.py", cwd=self.cwd, env={})
        self.assertTrue(ok_before)

        marker.write_text('{"run_id": "abc", "status": "RUNNING"}', encoding="utf-8")
        ok_after, reason = evaluate_file_edit("src/api/app.py", cwd=self.cwd, env={})
        self.assertFalse(ok_after)
        self.assertIn("受控研发流", reason)

    def test_cli_hook_exit_codes_and_fail_closed_json(self) -> None:
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = {
            **os.environ,
            "PYTHONPATH": src_dir,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

        # Malformed JSON -> Fail-Closed exit code 2
        bad = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.guard"],
            input="{broken json",
            text=True,
            capture_output=True,
            cwd=str(self.cwd),
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("Fail-Closed", bad.stderr)

        # Blocked protected file for worker or during active flow -> exit code 2
        env_worker = {**env, "ADK_WORKER_TOKEN": "run-1"}
        blocked = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.guard"],
            input=json.dumps({"tool_input": {"file_path": str(self.cwd / ".gitlab-ci.yml")}}),
            text=True,
            capture_output=True,
            cwd=str(self.cwd),
            env=env_worker,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(blocked.returncode, 2)

        # Allowed edit when no active run -> exit code 0
        allowed = subprocess.run(
            [sys.executable, "-m", "adk_dev_flow.guard"],
            input=json.dumps({"tool_input": {"file_path": str(self.cwd / "src" / "api" / "a.py")}}),
            text=True,
            capture_output=True,
            cwd=str(self.cwd),
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(allowed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
