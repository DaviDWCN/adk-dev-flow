from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.cli import init_target_repo
from adk_dev_flow.forge_client import GitLabClient, get_git_forge, NullGitForge
from adk_dev_flow.mcp_server import create_mcp_server


class TestFixesP0Part2(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        # Initialize bare mock git dir
        (self.root / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_p0_4_init_gitignores_only_runtime_artifacts_not_specs(self) -> None:
        """P0-4: scaffold_adk_dev_flow must NOT gitignore .adk/ entirely, but only state/worktrees."""
        actions = init_target_repo(self.root, force=True)
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")

        self.assertNotIn(".adk/\n", gitignore, "Must NOT gitignore entire .adk/ directory")
        self.assertIn(".adk/state.db*", gitignore)
        self.assertIn(".adk/active_run.json", gitignore)
        self.assertIn(".adk/worktrees/", gitignore)

    def test_p0_5_internal_gitlab_domain_detection(self) -> None:
        """P0-5: Custom domain GitLab remote must be detected via CI_SERVER_URL or GITLAB_URL."""
        with patch.dict(os.environ, {
            "CI_SERVER_URL": "https://git.corp.example.com",
            "CI_JOB_TOKEN": "gl-token-123",
            "CI_PROJECT_ID": "42",
        }, clear=False):
            gl = GitLabClient()
            self.assertTrue(gl.enabled)
            self.assertEqual(gl.url, "https://git.corp.example.com")
            self.assertEqual(gl.token, "gl-token-123")
            self.assertEqual(gl.project_id, "42")

        # When remote is git.corp.example.com and GITLAB_URL is set, get_git_forge should return GitLabClient
        with patch("adk_dev_flow.forge_client.get_git_remote_url", return_value="git@git.corp.example.com:group/proj.git"):
            with patch.dict(os.environ, {"GITLAB_URL": "https://git.corp.example.com", "GITLAB_TOKEN": "tok"}, clear=False):
                forge = get_git_forge(self.root)
                self.assertIsInstance(forge, GitLabClient)
                self.assertNotIsInstance(forge, NullGitForge)

    async def test_n2_and_p1_6_flow_init_preflight_and_repo_root(self) -> None:
        """N2 & P1-6: flow_init checks .git, returns repo_root and state_db_path."""
        _, service = create_mcp_server(repo_root=self.root)
        res = await service.flow_init("Test requirement")
        self.assertEqual(res["repo_root"], str(self.root))
        self.assertIn(".adk", res["state_db_path"])

        # When .git is absent, flow_init must fail with clear error
        non_git_tmp = tempfile.TemporaryDirectory()
        try:
            non_git_path = Path(non_git_tmp.name).resolve()
            _, bad_service = create_mcp_server(repo_root=non_git_path)
            bad_res = await bad_service.flow_init("Fail requirement")
            self.assertIn("error", bad_res)
            self.assertIn(".git", bad_res["error"])
        finally:
            non_git_tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
