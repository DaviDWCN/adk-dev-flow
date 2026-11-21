"""adk-dev-flow: Deterministic AI-assisted Python development workflow."""

from adk_dev_flow.forge_client import (
    BaseGitForge,
    GitHubClient,
    GitLabClient,
    NullGitForge,
    get_git_forge,
)

__version__ = "0.2.0"

try:
    from .agent import root_agent
except Exception:
    root_agent = None  # type: ignore

__all__ = [
    "__version__",
    "root_agent",
    "BaseGitForge",
    "GitHubClient",
    "GitLabClient",
    "NullGitForge",
    "get_git_forge",
]
