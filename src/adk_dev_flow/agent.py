"""Google ADK Agent definition for adk-dev-flow.

Exposes `root_agent` so that `adk web` can discover and serve this agent
in the official Google ADK Web UI.
"""

import os
from pathlib import Path
from typing import Any
from google.adk import Agent
from adk_dev_flow.gitlab_client import GitLabClient
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.state_store import StateStore


def _get_service() -> FlowService:
    root = Path(os.environ.get("REPO_ROOT", os.getcwd())).resolve()
    return FlowService(root, StateStore(root), GitLabClient())


def check_status(run_id: str) -> dict[str, Any]:
    """Check the status, current phase, and error logs of a workflow run."""
    return _get_service().flow_status(run_id)


def list_active_runs() -> dict[str, Any]:
    """List all currently active pipeline runs across the repository."""
    return _get_service().flow_active_runs()


def list_recent_runs(limit: int = 10) -> dict[str, Any]:
    """List recent workflow runs from the SQLite state store."""
    return _get_service().flow_list(limit)


root_agent = Agent(
    name="adk_dev_flow",
    description=(
        "Deterministic AI-assisted Python development workflow with worktree "
        "isolation, TDD Red acceptance contracts, critic verification, and GitLab MR automation."
    ),
    instruction=(
        "You are the ADK Dev Flow coordinator. You monitor and manage deterministic "
        "development runs. Use your tools to check pipeline statuses, track active runs, "
        "and inspect workflow health."
    ),
    tools=[check_status, list_active_runs, list_recent_runs],
)
