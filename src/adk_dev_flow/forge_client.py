"""Git forge abstraction supporting adaptive auto-detection for GitLab and GitHub."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
from pathlib import Path
import re
import subprocess
from typing import Any

import httpx

_DRAFT_PREFIX_RE = re.compile(r"^(?:draft|wip)\s*:\s*", re.IGNORECASE)
_GITHUB_REMOTE_RE = re.compile(
    r"(?:git@[\w.-]+:|https?://[^/]+/)([\w.-]+/[\w.-]+?)(?:\.git)?$", re.IGNORECASE
)


def strip_draft_prefix(title: str) -> str:
    """Strip leading 'Draft: ' or 'WIP: ' prefixes case-insensitively."""
    return _DRAFT_PREFIX_RE.sub("", title.strip(), count=1)


def parse_repo_from_remote(remote_url: str) -> str | None:
    """Parse 'owner/repo' path from a git remote URL (HTTPS or SSH)."""
    cleaned = remote_url.strip()
    match = _GITHUB_REMOTE_RE.search(cleaned)
    if match:
        return match.group(1).rstrip("/")
    return None


def get_git_remote_url(repo_root: Path | None = None) -> str:
    """Safely query 'git config --get remote.origin.url' for the given directory."""
    try:
        cmd = ["git", "config", "--get", "remote.origin.url"]
        res = subprocess.run(
            cmd,
            cwd=str(repo_root) if repo_root else os.getcwd(),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


class BaseGitForge(ABC):
    """Abstract interface representing a Git code hosting forge (GitLab, GitHub, etc.)."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Name of the platform ('gitlab', 'github', 'null')."""
        ...

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """True if the forge client is fully configured and ready to communicate."""
        ...

    @abstractmethod
    def get_issue(self, issue_iid: int) -> dict[str, Any]:
        """Fetch issue title, description, and metadata by issue number/id."""
        ...

    @abstractmethod
    def create_or_update_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        """Create or update a Merge Request (GitLab) or Pull Request (GitHub)."""
        ...

    @abstractmethod
    def post_mr_note(self, mr_iid: int, body: str) -> None:
        """Post a comment or review note under the MR / PR."""
        ...

    @abstractmethod
    def mark_mr_ready(self, mr_iid: int) -> dict[str, Any]:
        """Remove draft status from the MR / PR, marking it ready for review."""
        ...

    def create_or_update_pr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        """Alias for create_or_update_mr using GitHub PR terminology."""
        return self.create_or_update_mr(
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
            draft=draft,
        )

    def post_pr_note(self, mr_iid: int, body: str) -> None:
        """Alias for post_mr_note using GitHub PR terminology."""
        return self.post_mr_note(mr_iid, body)

    def mark_pr_ready(self, mr_iid: int) -> dict[str, Any]:
        """Alias for mark_mr_ready using GitHub PR terminology."""
        return self.mark_mr_ready(mr_iid)


class NullGitForge(BaseGitForge):
    """Null forge used when neither GitLab nor GitHub is configured (offline mode)."""

    @property
    def platform_name(self) -> str:
        return "null"

    @property
    def enabled(self) -> bool:
        return False

    def get_issue(self, issue_iid: int) -> dict[str, Any]:
        return {"iid": issue_iid, "title": "", "description": "", "labels": [], "web_url": ""}

    def create_or_update_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        return {"iid": 0, "web_url": "", "title": title}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        pass

    def mark_mr_ready(self, mr_iid: int) -> dict[str, Any]:
        return {"iid": mr_iid, "web_url": "", "title": ""}


class GitLabClient(BaseGitForge):
    """GitLab forge implementation backed by python-gitlab."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        project_id: str | None = None,
        ca_bundle: str | None = None,
        ssl_verify: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        self.url = (
            url
            or os.environ.get("GITLAB_URL", "")
            or os.environ.get("CI_SERVER_URL", "")
        ).strip()
        self.token = (
            token
            or os.environ.get("GITLAB_TOKEN", "")
            or os.environ.get("CI_JOB_TOKEN", "")
        ).strip()
        self.project_id = (
            project_id
            or os.environ.get("GITLAB_PROJECT_ID", "")
            or os.environ.get("CI_PROJECT_ID", "")
        ).strip()
        self.ca_bundle = (ca_bundle or os.environ.get("GITLAB_CA_BUNDLE", "")).strip()
        self.ssl_verify_flag = (
            ssl_verify
            if ssl_verify is not None
            else (os.environ.get("GITLAB_SSL_VERIFY", "true").strip().lower() != "false")
        )
        self.timeout = float(timeout or os.environ.get("GITLAB_TIMEOUT", "30"))
        self._project: Any = None

    @property
    def platform_name(self) -> str:
        return "gitlab"

    @property
    def ssl_verify(self) -> bool | str:
        if self.ca_bundle:
            return self.ca_bundle
        return self.ssl_verify_flag

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token and self.project_id)

    def _get_project(self) -> Any:
        if not self.enabled:
            raise RuntimeError("请配置环境变量 GITLAB_URL / GITLAB_TOKEN / GITLAB_PROJECT_ID")
        if self._project is None:
            import gitlab

            gl = gitlab.Gitlab(
                url=self.url,
                private_token=self.token,
                ssl_verify=self.ssl_verify,
                timeout=self.timeout,
            )
            self._project = gl.projects.get(self.project_id)
        return self._project

    def get_issue(self, issue_iid: int) -> dict[str, Any]:
        issue = self._get_project().issues.get(issue_iid)
        return {
            "iid": issue.iid,
            "title": issue.title,
            "description": issue.description or "",
            "labels": getattr(issue, "labels", []),
            "web_url": getattr(issue, "web_url", ""),
        }

    def create_or_update_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        project = self._get_project()
        clean_title = strip_draft_prefix(title)
        full_title = f"Draft: {clean_title}" if draft else clean_title
        existing = project.mergerequests.list(source_branch=source_branch, state="opened")
        if existing:
            mr = existing[0]
            mr.title = full_title
            mr.description = description
            mr.save()
        else:
            mr = project.mergerequests.create(
                {
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": full_title,
                    "description": description,
                    "remove_source_branch": True,
                }
            )
        return {"iid": mr.iid, "web_url": mr.web_url, "title": mr.title}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        mr = self._get_project().mergerequests.get(mr_iid)
        mr.notes.create({"body": body})

    def mark_mr_ready(self, mr_iid: int) -> dict[str, Any]:
        mr = self._get_project().mergerequests.get(mr_iid)
        cleaned = strip_draft_prefix(mr.title)
        if cleaned != mr.title:
            mr.title = cleaned
            mr.save()
        return {"iid": mr.iid, "web_url": mr.web_url, "title": mr.title}


class GitHubClient(BaseGitForge):
    """GitHub forge implementation backed by httpx calling GitHub REST & GraphQL APIs."""

    def __init__(
        self,
        token: str | None = None,
        repository: str | None = None,
        api_url: str | None = None,
        timeout: float | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.token = (
            token
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or ""
        ).strip()
        self.api_url = (
            api_url
            or os.environ.get("GITHUB_API_URL")
            or "https://api.github.com"
        ).rstrip("/")
        self.timeout = float(timeout or os.environ.get("GITHUB_TIMEOUT", "30"))

        raw_repo = (repository or os.environ.get("GITHUB_REPOSITORY") or "").strip()
        if not raw_repo:
            remote_url = get_git_remote_url(repo_root)
            raw_repo = parse_repo_from_remote(remote_url) or ""
        self.repo = raw_repo

    @property
    def platform_name(self) -> str:
        return "github"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_issue(self, issue_iid: int) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(f"GitHub 未配置 GITHUB_TOKEN 或 GITHUB_REPOSITORY (当前 repo={self.repo})")
        url = f"{self.api_url}/repos/{self.repo}/issues/{issue_iid}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        labels = [
            lbl["name"] if isinstance(lbl, dict) else str(lbl)
            for lbl in data.get("labels", [])
        ]
        return {
            "iid": data["number"],
            "title": data.get("title", ""),
            "description": data.get("body") or "",
            "labels": labels,
            "web_url": data.get("html_url", ""),
        }

    def create_or_update_mr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(f"GitHub 未配置 GITHUB_TOKEN 或 GITHUB_REPOSITORY (当前 repo={self.repo})")

        clean_title = strip_draft_prefix(title)
        full_title = f"Draft: {clean_title}" if draft else clean_title

        with httpx.Client(timeout=self.timeout) as client:
            # Check if PR exists for this head branch
            head_query = f"{self.repo.split('/')[0]}:{source_branch}" if "/" in self.repo else source_branch
            list_url = f"{self.api_url}/repos/{self.repo}/pulls?head={head_query}&state=open"
            list_resp = client.get(list_url, headers=self._headers())

            existing_pr: dict[str, Any] | None = None
            if list_resp.status_code == 200:
                prs = list_resp.json()
                if prs and isinstance(prs, list):
                    existing_pr = prs[0]

            if existing_pr:
                pr_number = existing_pr["number"]
                patch_url = f"{self.api_url}/repos/{self.repo}/pulls/{pr_number}"
                patch_resp = client.patch(
                    patch_url,
                    json={"title": full_title, "body": description},
                    headers=self._headers(),
                )
                patch_resp.raise_for_status()
                data = patch_resp.json()
            else:
                create_url = f"{self.api_url}/repos/{self.repo}/pulls"
                create_payload = {
                    "title": full_title,
                    "body": description,
                    "head": source_branch,
                    "base": target_branch,
                    "draft": draft,
                }
                create_resp = client.post(
                    create_url,
                    json=create_payload,
                    headers=self._headers(),
                )
                create_resp.raise_for_status()
                data = create_resp.json()

        return {
            "iid": data["number"],
            "web_url": data.get("html_url", ""),
            "title": data.get("title", ""),
            "node_id": data.get("node_id"),
        }

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        if not self.enabled:
            raise RuntimeError(f"GitHub 未配置 GITHUB_TOKEN 或 GITHUB_REPOSITORY (当前 repo={self.repo})")
        url = f"{self.api_url}/repos/{self.repo}/issues/{mr_iid}/comments"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json={"body": body}, headers=self._headers())
            resp.raise_for_status()

    def mark_mr_ready(self, mr_iid: int) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(f"GitHub 未配置 GITHUB_TOKEN 或 GITHUB_REPOSITORY (当前 repo={self.repo})")

        with httpx.Client(timeout=self.timeout) as client:
            get_url = f"{self.api_url}/repos/{self.repo}/pulls/{mr_iid}"
            resp = client.get(get_url, headers=self._headers())
            resp.raise_for_status()
            pr_data = resp.json()

            clean_title = strip_draft_prefix(pr_data.get("title", ""))
            # Update title if it had draft prefix
            if clean_title != pr_data.get("title"):
                client.patch(
                    get_url,
                    json={"title": clean_title},
                    headers=self._headers(),
                )

            # If draft is true, convert using GraphQL mutation
            if pr_data.get("draft") and pr_data.get("node_id"):
                graphql_url = (
                    f"{self.api_url}/graphql"
                    if "api.github.com" not in self.api_url
                    else "https://api.github.com/graphql"
                )
                mutation = """
                mutation($prId: ID!) {
                  markPullRequestReadyForReview(input: {pullRequestId: $prId}) {
                    pullRequest { id }
                  }
                }
                """
                try:
                    client.post(
                        graphql_url,
                        json={"query": mutation, "variables": {"prId": pr_data["node_id"]}},
                        headers=self._headers(),
                    )
                except Exception:
                    pass

        return {
            "iid": mr_iid,
            "web_url": pr_data.get("html_url", ""),
            "title": clean_title,
        }


def get_git_forge(repo_root: Path | None = None) -> BaseGitForge:
    """Adaptive auto-detection factory for Git code hosting forge (GitHub vs GitLab vs Null)."""
    # 1. Explicit environment override
    override = (
        os.environ.get("GIT_FORGE_PROVIDER")
        or os.environ.get("GIT_PROVIDER")
        or ""
    ).strip().lower()

    if override == "github":
        return GitHubClient(repo_root=repo_root)
    if override == "gitlab":
        return GitLabClient()
    if override in ("none", "null", "local"):
        return NullGitForge()

    # 2. Inspect git remote URL
    remote_url = get_git_remote_url(repo_root)
    if remote_url:
        lower_remote = remote_url.lower()
        if "github.com" in lower_remote:
            gh_client = GitHubClient(repo_root=repo_root)
            if gh_client.enabled or gh_client.token:
                return gh_client
        if "gitlab" in lower_remote:
            gl_client = GitLabClient()
            if gl_client.enabled or gl_client.token:
                return gl_client
        configured_gl = os.environ.get("GITLAB_URL", "") or os.environ.get("CI_SERVER_URL", "")
        if configured_gl:
            from urllib.parse import urlparse
            gl_host = urlparse(configured_gl).netloc.lower()
            if gl_host and gl_host in lower_remote:
                gl_client = GitLabClient()
                if gl_client.enabled or gl_client.token:
                    return gl_client

    # 3. Environment heuristic inspection
    gh_candidate = GitHubClient(repo_root=repo_root)
    gl_candidate = GitLabClient()

    if gh_candidate.enabled and not gl_candidate.enabled:
        return gh_candidate
    if gl_candidate.enabled and not gh_candidate.enabled:
        return gl_candidate
    if gh_candidate.enabled and gl_candidate.enabled:
        if remote_url and "github.com" in remote_url.lower():
            return gh_candidate
        return gl_candidate

    if gh_candidate.token:
        return gh_candidate
    if gl_candidate.token or gl_candidate.url:
        return gl_candidate

    # 4. Fallback to Null (offline local mode)
    return NullGitForge()
