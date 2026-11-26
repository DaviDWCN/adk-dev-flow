from __future__ import annotations

import os
import subprocess
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
from adk_dev_flow.mcp_server import FlowService, create_mcp_server, parse_epic_into_subtasks
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


class TestParseEpicIntoSubtasks(unittest.TestCase):
    """拆单正则只认行首（或分号之后）的列表项，正文里的标点不算。"""

    @staticmethod
    def _titles(epic: str) -> list[str]:
        return [task["title"] for task in parse_epic_into_subtasks(epic)]

    def test_hyphen_inside_a_word_is_not_a_bullet(self) -> None:
        epic = "改动后同步更新 docs/cpg-cas-architecture.md 的现状一节。\n1. fix(cpg): 甲\n2. feat(cpg): 乙"
        self.assertEqual(self._titles(epic), ["fix(cpg): 甲", "feat(cpg): 乙"])

    def test_decimal_number_in_preamble_is_not_an_item(self) -> None:
        epic = "实测 0.36 秒。\n1. fix(cpg): 甲\n2. feat(cpg): 乙"
        self.assertEqual(self._titles(epic), ["fix(cpg): 甲", "feat(cpg): 乙"])

    def test_inline_numbered_items_separated_by_semicolon(self) -> None:
        self.assertEqual(self._titles("1. xxx; 2. yyy"), ["feat: xxx", "feat: yyy"])

    def test_dash_and_star_bullets_at_line_start(self) -> None:
        self.assertEqual(self._titles("- xxx\n* yyy"), ["feat: xxx", "feat: yyy"])

    def test_indented_numbered_items(self) -> None:
        self.assertEqual(self._titles("  1. xxx\n  2. yyy"), ["feat: xxx", "feat: yyy"])

    def test_single_sentence_stays_one_task(self) -> None:
        self.assertEqual(self._titles("只有一句话，没有列表"), ["feat: 只有一句话，没有列表"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _rev(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class _RecordingForge(NullGitForge):
    """NullGitForge with enabled=True — records comments/closes for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.comments: list[tuple[int, str]] = []
        self.close_calls: list[int] = []

    @property
    def enabled(self) -> bool:
        return True

    def post_issue_comment(self, issue_iid: int, body: str) -> None:
        self.comments.append((issue_iid, body))

    def close_issue(self, issue_iid: int) -> dict:
        self.close_calls.append(issue_iid)
        return super().close_issue(issue_iid)


class TestFlowCloseIssue(unittest.IsolatedAsyncioTestCase):
    """flow_close_issue：合并落地后 fail-closed 关闭关联 issue。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        _git(self.repo, "init", "-q", "--initial-branch=main")
        (self.repo / "README.md").write_text("demo", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.store = StateStore(self.repo / "state.db")
        self.forge = _RecordingForge()
        self.forge.create_issue("fix: demo issue", "desc")  # iid 1, state "opened"
        self.service = FlowService(
            repo_root=self.repo,
            store=self.store,
            forge=self.forge,
            recovery_grace_period=0.0,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _head(self) -> str:
        return _rev(self.repo, "rev-parse", "HEAD")

    def _mr_ready_run(self, run_id: str, head_sha: str | None, base_sha: str | None = None) -> None:
        self.store.create_run(run_id, issue_iid=1, status="VERIFYING", branch="feat/demo")
        fields: dict = {"status": "MR_READY"}
        if head_sha:
            fields["head_sha"] = head_sha
        if base_sha:
            fields["base_sha"] = base_sha
        self.store.update_run(run_id, **fields)

    async def test_closes_issue_when_merge_landed(self) -> None:
        head = self._head()
        self._mr_ready_run("r-ok", head_sha=head, base_sha=head)
        res = await self.service.flow_close_issue("r-ok")
        self.assertTrue(res.get("closed"), res)
        self.assertNotIn("errors", res)
        self.assertEqual(self.forge.get_issue(1)["state"], "closed")
        self.assertEqual(self.forge.close_calls, [1])
        self.assertEqual(len(self.forge.comments), 1)
        self.assertIn("r-ok", self.forge.comments[0][1])

    async def test_refuses_when_merge_not_landed(self) -> None:
        # 与 HEAD 无关的孤儿提交（同 tree、无父）——不是 HEAD 祖先
        tree = _rev(self.repo, "rev-parse", "HEAD^{tree}")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        orphan = subprocess.run(
            ["git", "-C", str(self.repo), "commit-tree", tree],
            check=True, capture_output=True, text=True, env=env,
        ).stdout.strip()
        self._mr_ready_run("r-orphan", head_sha=orphan)
        res = await self.service.flow_close_issue("r-orphan")
        self.assertFalse(res.get("closed"))
        self.assertIn("fail-closed", res.get("reason", ""))
        self.assertEqual(self.forge.get_issue(1)["state"], "opened")
        self.assertEqual(self.forge.close_calls, [])

    async def test_idempotent_when_already_closed(self) -> None:
        self.forge.close_issue(1)
        self._mr_ready_run("r-closed", head_sha=self._head())
        res = await self.service.flow_close_issue("r-closed")
        self.assertTrue(res.get("closed"))
        self.assertTrue(res.get("already_closed"))

    async def test_refuses_offline_forge_even_when_merged(self) -> None:
        offline = NullGitForge()
        self.service.gl = offline
        self.service.forge = offline
        self._mr_ready_run("r-off", head_sha=self._head())
        res = await self.service.flow_close_issue("r-off")
        self.assertFalse(res.get("closed"))
        self.assertTrue(res.get("merge_verified"))
        self.assertIn("离线", res.get("reason", ""))
        self.assertEqual(self.forge.get_issue(1)["state"], "opened")
        self.assertEqual(self.forge.close_calls, [])

    async def test_refuses_non_ready_run(self) -> None:
        self.store.create_run("r-v", issue_iid=1, status="VERIFYING", branch="feat/demo")
        res = await self.service.flow_close_issue("r-v")
        self.assertIn("error", res)

    async def test_missing_head_sha_and_branch_deleted(self) -> None:
        self._mr_ready_run("r-old", head_sha=None)  # 分支 feat/demo 在仓库中不存在
        res = await self.service.flow_close_issue("r-old")
        self.assertIn("error", res)
        self.assertIn("head_sha", res["error"])


if __name__ == "__main__":
    unittest.main()
