"""adk-dev-flow: Deterministic AI-assisted Python development workflow."""

from adk_dev_flow.forge_client import (
    BaseGitForge,
    GitHubClient,
    GitLabClient,
    NullGitForge,
    get_git_forge,
)

__version__ = "0.2.2"


def __getattr__(name: str):
    if name in ("create_dashboard_app", "get_dashboard_html"):
        from adk_dev_flow.dashboard import create_dashboard_app, get_dashboard_html

        if name == "create_dashboard_app":
            return create_dashboard_app
        return get_dashboard_html
    if name == "root_agent":
        try:
            from .agent import root_agent

            return root_agent
        except Exception:
            return None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "root_agent",
    "BaseGitForge",
    "GitHubClient",
    "GitLabClient",
    "NullGitForge",
    "get_git_forge",
    "get_dashboard_html",
    "create_dashboard_app",
]
