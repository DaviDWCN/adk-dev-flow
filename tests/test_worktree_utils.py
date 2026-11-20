from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from adk_dev_flow.worktree_utils import (
    audit_worktree_changes,
    compute_error_signature,
    compute_file_sha256,
    parse_review_verdict_dict,
    resolve_tool_command,
)


class TestWorktreeUtils(unittest.IsolatedAsyncioTestCase):
    def test_error_signature_ignores_volatile_timings_and_paths(self) -> None:
        wt = Path("/tmp/worktree-run-123")
        err1 = (
            "[pytest exit=1]\n"
            f"FAILED {wt.resolve()}/tests/test_app.py::test_sum - AssertionError: 1 != 2\n"
            "object at 0x7f9a1234abcd\n"
            "1 failed in 0.42s\n"
        )
        err2 = (
            "[pytest exit=1]\n"
            f"FAILED {wt.resolve()}/tests/test_app.py::test_sum - AssertionError: 1 != 2\n"
            "object at 0x7f9a9999ef01\n"
            "1 failed in 1.87s\n"
        )
        err_diff = (
            "[pytest exit=1]\n"
            f"FAILED {wt.resolve()}/tests/test_app.py::test_sum - ValueError: bad input\n"
            "1 failed in 0.42s\n"
        )
        self.assertEqual(
            compute_error_signature(err1, worktree=wt),
            compute_error_signature(err2, worktree=wt),
        )
        self.assertNotEqual(
            compute_error_signature(err1, worktree=wt),
            compute_error_signature(err_diff, worktree=wt),
        )

    def test_parse_review_verdict_fail_closed(self) -> None:
        # Valid dict
        v_ok = parse_review_verdict_dict({"approved": True, "blocking_issues": [], "summary": "LGTM"})
        self.assertTrue(v_ok["approved"])

        # Valid fenced JSON string (rejected)
        fenced = '```json\n{"approved": false, "blocking_issues": ["SQLi"], "summary": "unsafe"}\n```'
        v_rej = parse_review_verdict_dict(fenced)
        self.assertFalse(v_rej["approved"])
        self.assertEqual(v_rej["blocking_issues"], ["SQLi"])

        # ADK Content-like object with .parts[0].text
        adk_content = SimpleNamespace(
            parts=[SimpleNamespace(text='{"approved": false, "blocking_issues": ["bug"], "summary": "bad"}')]
        )
        v_content = parse_review_verdict_dict(adk_content)
        self.assertFalse(v_content["approved"])
        self.assertEqual(v_content["blocking_issues"], ["bug"])

        # Model-like object with model_dump()
        model_obj = SimpleNamespace(
            model_dump=lambda: {"approved": True, "blocking_issues": [], "summary": "ok"}
        )
        self.assertTrue(parse_review_verdict_dict(model_obj)["approved"])

        # Malformed prose -> MUST be Fail-Closed (approved=False)
        v_bad = parse_review_verdict_dict("Looks mostly fine to me!")
        self.assertFalse(v_bad["approved"])
        self.assertTrue(len(v_bad["blocking_issues"]) >= 1)

        # Missing boolean approved -> Fail-Closed
        v_missing = parse_review_verdict_dict({"summary": "forgot approved field"})
        self.assertFalse(v_missing["approved"])

        # Unexpected type -> Fail-Closed
        self.assertFalse(parse_review_verdict_dict(None)["approved"])

    def test_resolve_tool_command_prefers_target_venv_and_supports_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            worktree = repo_root / ".adk" / "worktrees" / "r1"
            (repo_root / ".venv" / "bin").mkdir(parents=True)
            worktree.mkdir(parents=True)

            fake_pytest = repo_root / ".venv" / "bin" / "pytest"
            fake_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_pytest.chmod(fake_pytest.stat().st_mode | stat.S_IXUSR)

            # Should find repo_root/.venv/bin/pytest even though worktree has no .venv
            cmd = resolve_tool_command(
                "pytest", ["-q", "tests/test_a.py"], worktree=worktree, repo_root=repo_root
            )
            self.assertEqual(cmd, [str(fake_pytest), "-q", "tests/test_a.py"])

            # Explicit override with placeholder
            cmd_override = resolve_tool_command(
                "pytest",
                ["-q"],
                worktree=worktree,
                repo_root=repo_root,
                env_override="python -m pytest -vv {test_path}",
                placeholder_vars={"test_path": "tests/test_a.py"},
            )
            self.assertEqual(cmd_override, ["python", "-m", "pytest", "-vv", "tests/test_a.py"])

            # Skip returns None
            self.assertIsNone(
                resolve_tool_command(
                    "ruff", ["check", "."], worktree=worktree, repo_root=repo_root, env_override="skip"
                )
            )

    async def test_audit_worktree_changes_on_real_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            def git(*args: str, cwd: Path = repo) -> str:
                res = subprocess.run(
                    ["git", *args],
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    check=True,
                    env={
                        **os.environ,
                        "GIT_AUTHOR_NAME": "Tester",
                        "GIT_AUTHOR_EMAIL": "tester@example.com",
                        "GIT_COMMITTER_NAME": "Tester",
                        "GIT_COMMITTER_EMAIL": "tester@example.com",
                    },
                )
                return res.stdout.strip()

            git("init")
            (repo / "src").mkdir()
            (repo / "other").mkdir()
            (repo / "tests").mkdir()
            (repo / "src" / "app.py").write_text("def add(a, b): return 0\n", encoding="utf-8")
            (repo / "other" / "secret.py").write_text("KEY = 1\n", encoding="utf-8")
            (repo / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-m", "init")

            # Add locked acceptance test commit
            test_path = "tests/test_add.py"
            original_test_code = "from src.app import add\ndef test_add(): assert add(1, 2) == 3\n"
            (repo / test_path).write_text(original_test_code, encoding="utf-8")
            locked_sha = compute_file_sha256(repo / test_path)
            git("add", test_path)
            git("commit", "-m", "lock test")
            lock_commit = git("rev-parse", "HEAD")

            # 1) Valid edit inside allowed_paths -> ok=True
            (repo / "src" / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
            audit_ok = await audit_worktree_changes(
                repo,
                allowed_paths=["src"],
                acceptance_test_path=test_path,
                locked_test_sha256=locked_sha,
                locked_test_commit_sha=lock_commit,
            )
            self.assertTrue(audit_ok.ok, audit_ok.violations)

            # 2) Worker tampers with locked test via Bash -> detected AND auto-restored
            (repo / test_path).write_text("def test_add(): assert True\n", encoding="utf-8")
            audit_tamper = await audit_worktree_changes(
                repo,
                allowed_paths=["src"],
                acceptance_test_path=test_path,
                locked_test_sha256=locked_sha,
                locked_test_commit_sha=lock_commit,
            )
            self.assertFalse(audit_tamper.ok)
            self.assertTrue(audit_tamper.locked_test_tampered)
            self.assertEqual((repo / test_path).read_text(encoding="utf-8"), original_test_code)

            # 3) Worker modifies out-of-whitelist file AND commits it -> still caught via git diff lock_commit..HEAD
            (repo / "other" / "secret.py").write_text("KEY = 999\n", encoding="utf-8")
            git("add", "other/secret.py")
            git("commit", "-m", "sneaky commit")
            audit_sneaky = await audit_worktree_changes(
                repo,
                allowed_paths=["src"],
                acceptance_test_path=test_path,
                locked_test_sha256=locked_sha,
                locked_test_commit_sha=lock_commit,
            )
            self.assertFalse(audit_sneaky.ok)
            self.assertTrue(any("other/secret.py" in v for v in audit_sneaky.violations))

            # 4) Worker modifies protected dotfile .gitlab-ci.yml -> caught as protected violation
            (repo / ".gitlab-ci.yml").write_text("stages: []\n", encoding="utf-8")
            audit_protected = await audit_worktree_changes(
                repo,
                allowed_paths=["src"],
                acceptance_test_path=test_path,
                locked_test_sha256=locked_sha,
                locked_test_commit_sha=lock_commit,
            )
            self.assertFalse(audit_protected.ok)
            self.assertTrue(any("受保护" in v and ".gitlab-ci.yml" in v for v in audit_protected.violations))


if __name__ == "__main__":
    unittest.main()
