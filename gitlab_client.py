from __future__ import annotations

import os
from typing import Any

import gitlab


class GitLabClient:
    def __init__(self) -> None:
        self.url = os.environ.get("GITLAB_URL", "").strip()
        self.token = os.environ.get("GITLAB_TOKEN", "").strip()
        self.project_id = os.environ.get("GITLAB_PROJECT_ID", "").strip()
        self.ssl_verify = os.environ.get("GITLAB_SSL_VERIFY", "true").lower() != "false"
        self._project = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token and self.project_id)

    def _get_project(self):
        if not self.enabled:
            raise RuntimeError("请配置环境变量 GITLAB_URL / GITLAB_TOKEN / GITLAB_PROJECT_ID")
        if self._project is None:
            gl = gitlab.Gitlab(
                url=self.url,
                private_token=self.token,
                ssl_verify=self.ssl_verify,
            )
            self._project = gl.projects.get(self.project_id)
        return self._project

    def get_issue(self, issue_iid: int) -> dict[str, Any]:
        issue = self._get_project().issues.get(issue_iid)
        return {
            "iid": issue.iid,
            "title": issue.title,
            "description": issue.description or "",
            "labels": issue.labels,
            "web_url": issue.web_url,
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
        full_title = f"Draft: {title}" if draft and not title.startswith("Draft:") else title
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
        return {"iid": mr.iid, "web_url": mr.web_url}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        mr = self._get_project().mergerequests.get(mr_iid)
        mr.notes.create({"body": body})

    def mark_mr_ready(self, mr_iid: int) -> dict[str, Any]:
        mr = self._get_project().mergerequests.get(mr_iid)
        if mr.title.startswith("Draft: "):
            mr.title = mr.title.removeprefix("Draft: ")
            mr.save()
        return {"iid": mr.iid, "web_url": mr.web_url, "title": mr.title}
