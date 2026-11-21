from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import unittest

from adk_dev_flow.forge_client import (
    BaseGitForge,
    GitHubClient,
    GitLabClient,
    NullGitForge,
    get_git_forge,
    parse_repo_from_remote,
    strip_draft_prefix,
)


class TestForgeClients(unittest.TestCase):
    def test_strip_draft_prefix(self) -> None:
        self.assertEqual(strip_draft_prefix("Draft: Add feature"), "Add feature")
        self.assertEqual(strip_draft_prefix("draft:Fix bug"), "Fix bug")
        self.assertEqual(strip_draft_prefix("WIP: In progress"), "In progress")
        self.assertEqual(strip_draft_prefix("wip:In progress"), "In progress")
        self.assertEqual(strip_draft_prefix("feat: normal title"), "feat: normal title")

    def test_parse_repo_from_remote(self) -> None:
        self.assertEqual(
            parse_repo_from_remote("https://github.com/DaviDWCN/adk-dev-flow.git"),
            "DaviDWCN/adk-dev-flow",
        )
        self.assertEqual(
            parse_repo_from_remote("https://github.com/DaviDWCN/adk-dev-flow"),
            "DaviDWCN/adk-dev-flow",
        )
        self.assertEqual(
            parse_repo_from_remote("git@github.com:octocat/Hello-World.git"),
            "octocat/Hello-World",
        )
        self.assertEqual(
            parse_repo_from_remote("git@gitlab.internal.corp:devs/my-service.git"),
            "devs/my-service",
        )
        self.assertIsNone(parse_repo_from_remote("not_a_valid_git_url"))

    def test_null_git_forge(self) -> None:
        forge = NullGitForge()
        self.assertEqual(forge.platform_name, "null")
        self.assertFalse(forge.enabled)
        issue = forge.get_issue(123)
        self.assertEqual(issue["iid"], 123)
        mr = forge.create_or_update_mr("feat", "main", "Title", "Desc")
        self.assertEqual(mr["iid"], 0)
        forge.post_mr_note(0, "comment")
        ready = forge.mark_mr_ready(0)
        self.assertEqual(ready["iid"], 0)

    def test_gitlab_client_backward_compat(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITLAB_URL": "https://gitlab.example.com",
                "GITLAB_TOKEN": "gl-token-123",
                "GITLAB_PROJECT_ID": "1001",
            },
            clear=True,
        ):
            client = GitLabClient()
            self.assertEqual(client.platform_name, "gitlab")
            self.assertTrue(client.enabled)
            self.assertEqual(client.url, "https://gitlab.example.com")
            self.assertEqual(client.token, "gl-token-123")
            self.assertEqual(client.project_id, "1001")

    def test_github_client_init_and_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "ghp_secrettoken123",
                "GITHUB_REPOSITORY": "testorg/testrepo",
            },
            clear=True,
        ):
            client = GitHubClient()
            self.assertEqual(client.platform_name, "github")
            self.assertTrue(client.enabled)
            self.assertEqual(client.repo, "testorg/testrepo")
            self.assertEqual(client.token, "ghp_secrettoken123")

    @patch("httpx.Client")
    def test_github_client_get_issue(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "number": 42,
            "title": "Fix authentication crash",
            "body": "NullPointerException on login",
            "labels": [{"name": "bug"}, {"name": "p0"}],
            "html_url": "https://github.com/testorg/testrepo/issues/42",
        }
        mock_client.get.return_value = mock_resp

        client = GitHubClient(token="fake", repository="testorg/testrepo")
        issue = client.get_issue(42)

        self.assertEqual(issue["iid"], 42)
        self.assertEqual(issue["title"], "Fix authentication crash")
        self.assertEqual(issue["description"], "NullPointerException on login")
        self.assertEqual(issue["labels"], ["bug", "p0"])
        self.assertEqual(issue["web_url"], "https://github.com/testorg/testrepo/issues/42")

    @patch("httpx.Client")
    def test_github_client_create_or_update_pr(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        # 1. New PR creation
        mock_list_resp = MagicMock()
        mock_list_resp.status_code = 200
        mock_list_resp.json.return_value = []
        mock_client.get.return_value = mock_list_resp

        mock_create_resp = MagicMock()
        mock_create_resp.status_code = 201
        mock_create_resp.json.return_value = {
            "number": 101,
            "title": "Draft: feat: awesome feature",
            "html_url": "https://github.com/testorg/testrepo/pull/101",
            "node_id": "PR_node123",
        }
        mock_client.post.return_value = mock_create_resp

        client = GitHubClient(token="fake", repository="testorg/testrepo")
        res = client.create_or_update_mr(
            source_branch="feat/awesome",
            target_branch="main",
            title="feat: awesome feature",
            description="Detailed MR description",
            draft=True,
        )

        self.assertEqual(res["iid"], 101)
        self.assertEqual(res["web_url"], "https://github.com/testorg/testrepo/pull/101")
        self.assertEqual(res["title"], "Draft: feat: awesome feature")

    @patch("httpx.Client")
    def test_github_client_post_note_and_mark_ready(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        # Mock post comment
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 201
        mock_client.post.return_value = mock_post_resp

        client = GitHubClient(token="fake", repository="testorg/testrepo")
        client.post_mr_note(101, "Critic review: Approved")

        # Mock mark PR ready
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "number": 101,
            "title": "Draft: feat: awesome feature",
            "html_url": "https://github.com/testorg/testrepo/pull/101",
            "draft": True,
            "node_id": "PR_kwDO1234",
        }
        mock_client.get.return_value = mock_get_resp

        ready_res = client.mark_mr_ready(101)
        self.assertEqual(ready_res["iid"], 101)
        self.assertEqual(ready_res["title"], "feat: awesome feature")

    def test_adaptive_auto_detection_priority(self) -> None:
        # Priority 1: Explicit GIT_FORGE_PROVIDER
        with patch.dict(os.environ, {"GIT_FORGE_PROVIDER": "github", "GITHUB_TOKEN": "tok"}, clear=True):
            forge = get_git_forge()
            self.assertEqual(forge.platform_name, "github")

        with patch.dict(os.environ, {"GIT_FORGE_PROVIDER": "gitlab"}, clear=True):
            forge = get_git_forge()
            self.assertEqual(forge.platform_name, "gitlab")

        with patch.dict(os.environ, {"GIT_FORGE_PROVIDER": "none"}, clear=True):
            forge = get_git_forge()
            self.assertEqual(forge.platform_name, "null")

        # Priority 2: Remote URL inspection (when origin is github.com)
        with patch("adk_dev_flow.forge_client.get_git_remote_url", return_value="https://github.com/myorg/myrepo.git"):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "gh_secret"}, clear=True):
                forge = get_git_forge()
                self.assertEqual(forge.platform_name, "github")
                self.assertEqual(forge.repo, "myorg/myrepo")

        # Priority 3: Remote URL inspection (when origin is gitlab)
        with patch("adk_dev_flow.forge_client.get_git_remote_url", return_value="https://gitlab.internal.com/group/proj.git"):
            with patch.dict(os.environ, {"GITLAB_TOKEN": "gl_secret", "GITLAB_URL": "https://gitlab.internal.com"}, clear=True):
                forge = get_git_forge()
                self.assertEqual(forge.platform_name, "gitlab")

        # Fallback to Null when empty
        with patch("adk_dev_flow.forge_client.get_git_remote_url", return_value=""):
            with patch.dict(os.environ, {}, clear=True):
                forge = get_git_forge()
                self.assertEqual(forge.platform_name, "null")
                self.assertFalse(forge.enabled)


if __name__ == "__main__":
    unittest.main()
