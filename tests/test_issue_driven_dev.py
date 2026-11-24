from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import unittest

from starlette.testclient import TestClient

from adk_dev_flow.forge_client import (
    BaseGitForge,
    GitHubClient,
    GitLabClient,
    NullGitForge,
)
from adk_dev_flow.mcp_server import create_mcp_server
from adk_dev_flow.state_store import StateStore


class TestIssueDrivenDev(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp.name).resolve()
        (self.repo_root / ".git").mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self.repo_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_null_forge_create_and_close_issue(self) -> None:
        forge = NullGitForge()
        created = forge.create_issue(
            title="Add user authentication",
            description="Support JWT login and logout",
            labels=["auth", "feature"],
        )
        self.assertIn("iid", created)
        self.assertEqual(created["title"], "Add user authentication")
        self.assertEqual(created["description"], "Support JWT login and logout")
        self.assertEqual(created["labels"], ["auth", "feature"])

        forge.post_issue_comment(created["iid"], "Testing comment")

        closed = forge.close_issue(created["iid"])
        self.assertEqual(closed["iid"], created["iid"])
        self.assertEqual(closed["state"], "closed")

    @patch("httpx.Client")
    def test_github_create_and_close_issue(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        # Mock create issue
        mock_create_resp = MagicMock()
        mock_create_resp.status_code = 201
        mock_create_resp.json.return_value = {
            "number": 88,
            "title": "feat: AST mutation gate",
            "body": "Detailed mutation engine spec",
            "labels": [{"name": "ast"}, {"name": "p0"}],
            "html_url": "https://github.com/testorg/testrepo/issues/88",
        }
        mock_client.post.return_value = mock_create_resp

        client = GitHubClient(token="fake_token", repository="testorg/testrepo")
        created = client.create_issue(
            title="feat: AST mutation gate",
            description="Detailed mutation engine spec",
            labels=["ast", "p0"],
        )
        self.assertEqual(created["iid"], 88)
        self.assertEqual(created["web_url"], "https://github.com/testorg/testrepo/issues/88")
        self.assertEqual(created["labels"], ["ast", "p0"])

        # Mock post comment
        mock_comment_resp = MagicMock()
        mock_comment_resp.status_code = 201
        mock_client.post.return_value = mock_comment_resp
        client.post_issue_comment(88, "Pipeline run started")

        # Mock close issue
        mock_patch_resp = MagicMock()
        mock_patch_resp.status_code = 200
        mock_patch_resp.json.return_value = {
            "number": 88,
            "state": "closed",
            "html_url": "https://github.com/testorg/testrepo/issues/88",
        }
        mock_client.patch.return_value = mock_patch_resp
        closed = client.close_issue(88)
        self.assertEqual(closed["iid"], 88)
        self.assertEqual(closed["state"], "closed")

    def test_gitlab_create_and_close_issue(self) -> None:
        client = GitLabClient(
            url="https://gitlab.example.com",
            token="gl-fake",
            project_id="101",
        )
        mock_project = MagicMock()
        mock_issue = MagicMock()
        mock_issue.iid = 99
        mock_issue.title = "gl feature"
        mock_issue.description = "gl desc"
        mock_issue.labels = ["backend"]
        mock_issue.web_url = "https://gitlab.example.com/project/issues/99"

        mock_project.issues.create.return_value = mock_issue
        mock_project.issues.get.return_value = mock_issue
        client._project = mock_project

        created = client.create_issue("gl feature", "gl desc", ["backend"])
        self.assertEqual(created["iid"], 99)
        self.assertEqual(created["web_url"], "https://gitlab.example.com/project/issues/99")

        client.post_issue_comment(99, "Note from agent")
        mock_issue.notes.create.assert_called_once_with({"body": "Note from agent"})

        closed = client.close_issue(99)
        self.assertEqual(closed["iid"], 99)
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(mock_issue.state_event, "close")
        mock_issue.save.assert_called_once()

    async def test_mcp_flow_create_issue_and_plan_issues(self) -> None:
        mcp, service = create_mcp_server(repo_root=self.repo_root, store=self.store)

        # 1. flow_create_issue
        res = await service.flow_create_issue(
            title="Support payment webhook",
            description="Handle Stripe signature verification and webhook idempotency",
            labels=["payment", "webhook"],
        )
        self.assertTrue(res["ok"])
        self.assertIn("issue_iid", res)
        self.assertEqual(res["title"], "Support payment webhook")

        # 2. flow_plan_issues preview (auto_create=False)
        plan_res = await service.flow_plan_issues(
            epic_requirement="重构用户认证体系：1. 支持 OAuth2 登录；2. 增加基于 JWT 的会话刷新；3. 添加审计日志。",
            auto_create=False,
            labels=["epic-auth"],
        )
        self.assertTrue(plan_res["ok"])
        self.assertGreaterEqual(len(plan_res["planned_issues"]), 2)
        self.assertFalse(plan_res["auto_created"])

        # 3. flow_plan_issues auto_create=True
        auto_res = await service.flow_plan_issues(
            epic_requirement="重构用户认证体系：1. 支持 OAuth2 登录；2. 增加基于 JWT 的会话刷新；3. 添加审计日志。",
            auto_create=True,
            labels=["epic-auth"],
        )
        self.assertTrue(auto_res["ok"])
        self.assertTrue(auto_res["auto_created"])
        self.assertGreaterEqual(len(auto_res["created_issues"]), 2)
        for iss in auto_res["created_issues"]:
            self.assertIn("issue_iid", iss)
            self.assertIn("web_url", iss)

    async def test_flow_init_notifies_issue_comment(self) -> None:
        mock_forge = MagicMock()
        mock_forge.enabled = True
        mock_forge.platform_name = "gitlab"
        mock_forge.get_issue.return_value = {
            "iid": 12,
            "title": "Database connection pooling",
            "description": "Configure min/max pool size",
            "labels": ["db"],
            "web_url": "https://gitlab.example.com/proj/issues/12",
        }
        mcp, service = create_mcp_server(
            repo_root=self.repo_root,
            store=self.store,
            forge=mock_forge,
        )

        # Initialize flow with issue_iid
        init_res = await service.flow_init(
            requirement="Database connection pooling",
            issue_iid=12,
        )
        self.assertIn("run_id", init_res)
        self.assertEqual(init_res["status"], "SPEC_DRAFTING")

        # Verify forge.post_issue_comment was called
        mock_forge.post_issue_comment.assert_called_once()
        args, _ = mock_forge.post_issue_comment.call_args
        self.assertEqual(args[0], 12)
        self.assertIn("ADK Dev Flow", args[1])


if __name__ == "__main__":
    unittest.main()
