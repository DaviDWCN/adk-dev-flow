"""Backward-compatible proxy module for GitLabClient, now part of forge_client."""

from __future__ import annotations

from adk_dev_flow.forge_client import (
    BaseGitForge,
    GitHubClient,
    GitLabClient,
    NullGitForge,
    get_git_forge,
    strip_draft_prefix,
)

__all__ = [
    "BaseGitForge",
    "GitHubClient",
    "GitLabClient",
    "NullGitForge",
    "get_git_forge",
    "strip_draft_prefix",
]
