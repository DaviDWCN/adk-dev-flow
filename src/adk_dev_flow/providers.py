from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseWorkerProvider(ABC):
    """Abstract worker runtime provider interface for SWE agent execution."""

    @abstractmethod
    async def run_worker(
        self,
        *,
        worktree: Path,
        prompt: str,
        run_id: str,
        allowed_paths: list[str],
        locked_tests: list[str],
        max_turns: int = 15,
        timeout_sec: int = 300,
    ) -> dict[str, Any]:
        """Execute the worker agent inside the specified worktree."""
        pass


class CodeBuddyWorkerProvider(BaseWorkerProvider):
    """Default provider: executes headless CodeBuddy CLI inside the worktree."""

    async def run_worker(
        self,
        *,
        worktree: Path,
        prompt: str,
        run_id: str,
        allowed_paths: list[str],
        locked_tests: list[str],
        max_turns: int = 15,
        timeout_sec: int = 300,
    ) -> dict[str, Any]:
        from adk_dev_flow.codebuddy_bridge import run_codebuddy_worker

        return await run_codebuddy_worker(
            worktree=worktree,
            prompt=prompt,
            run_id=run_id,
            allowed_paths=allowed_paths,
            locked_tests=locked_tests,
            max_turns=max_turns,
            timeout_sec=timeout_sec,
        )


class MockWorkerProvider(BaseWorkerProvider):
    """Mock provider for zero-CLI testing and deterministic simulations."""

    def __init__(self, changed_files: list[str] | None = None, summary: str = "Mock completed"):
        self.changed_files = changed_files or []
        self.summary = summary

    async def run_worker(
        self,
        *,
        worktree: Path,
        prompt: str,
        run_id: str,
        allowed_paths: list[str],
        locked_tests: list[str],
        max_turns: int = 15,
        timeout_sec: int = 300,
    ) -> dict[str, Any]:
        return {
            "changed_files": self.changed_files,
            "summary": self.summary,
        }


def get_worker_provider(provider_type: str | None = None) -> BaseWorkerProvider:
    """Factory to obtain the configured worker provider."""
    p_type = (provider_type or os.environ.get("ADK_WORKER_PROVIDER", "codebuddy")).lower().strip()
    if p_type == "mock":
        return MockWorkerProvider()
    return CodeBuddyWorkerProvider()
