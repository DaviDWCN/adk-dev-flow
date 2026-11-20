from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from gitlab_client import GitLabClient
from models import SpecContract, StateStore
from workflow import execute_workflow_run

REPO_ROOT = Path(os.environ.get("REPO_ROOT", os.getcwd())).resolve()
STORE = StateStore(REPO_ROOT)
GL = GitLabClient()
BG_TASKS: dict[str, asyncio.Task[None]] = {}

mcp = FastMCP("adk-dev-flow")


@mcp.tool()
def flow_init(requirement: str, issue_iid: int | None = None) -> dict[str, Any]:
    """初始化确定性开发流。若提供内网 GitLab Issue 编号，将自动拉取详情。"""
    run_id = uuid.uuid4().hex[:12]
    issue_info: dict[str, Any] | None = None
    if issue_iid is not None and GL.enabled:
        issue_info = GL.get_issue(issue_iid)
    STORE.create_run(run_id=run_id, issue_iid=issue_iid)
    return {
        "run_id": run_id,
        "status": "SPEC_DRAFTING",
        "requirement": requirement,
        "gitlab_issue": issue_info,
        "next_step": (
            "请结合仓库结构起草 SpecContract（含验收测试源码），"
            "向用户展示并获得明确确认后再调用 flow_lock_spec。"
        ),
    }


@mcp.tool()
def flow_lock_spec(run_id: str, spec: SpecContract) -> dict[str, Any]:
    """校验并冻结用户已确认的结构化 Spec 与验收测试用例。"""
    run = STORE.get_run(run_id)
    if not run:
        return {"error": f"未知 run_id: {run_id}"}
    STORE.update_run(run_id, status="SPEC_LOCKED", spec_json=spec.model_dump_json())
    return {
        "run_id": run_id,
        "status": "SPEC_LOCKED",
        "locked_test": spec.acceptance_test_path,
        "allowed_paths": spec.allowed_paths,
        "next_step": "Spec 已冻结，可调用 flow_dispatch 触发 ADK 后台隔离执行与验收。",
    }


@mcp.tool()
async def flow_dispatch(run_id: str) -> dict[str, Any]:
    """在后台启动 ADK 2.x Graph Workflow（建 Worktree、隔离编码、ruff+pytest 验收与自愈、Critic 审查、建 MR）。"""
    run = STORE.get_run(run_id)
    if not run or run["status"] != "SPEC_LOCKED":
        cur = run["status"] if run else "None"
        return {"error": f"当前状态不可派发（需 SPEC_LOCKED，实际为 {cur}）"}

    task = asyncio.create_task(execute_workflow_run(REPO_ROOT, STORE, GL, run_id))
    BG_TASKS[run_id] = task
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "message": "ADK 确定性流水线已在后台隔离 Worktree 启动，请调用 flow_status 查询进度。",
    }


@mcp.tool()
def flow_status(run_id: str) -> dict[str, Any]:
    """查询确定性开发流的实时阶段、修复轮次、报错日志及 MR 链接。"""
    run = STORE.get_run(run_id)
    if not run:
        return {"error": f"未知 run_id: {run_id}"}
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "branch": run["branch"],
        "worktree_path": run["worktree_path"],
        "attempt": run["attempt"],
        "mr_iid": run["mr_iid"],
        "mr_url": run["mr_url"],
        "review": json.loads(run["review_json"]) if run["review_json"] else None,
        "last_error_log": (run["last_error_log"] or "")[-1500:],
    }


@mcp.tool()
def flow_finalize(run_id: str) -> dict[str, Any]:
    """确认无误后移除 Draft 标记，将内网 GitLab MR 置为 Ready 并释放本地工作流锁。"""
    run = STORE.get_run(run_id)
    if not run:
        return {"error": f"未知 run_id: {run_id}"}
    result: dict[str, Any] = {"run_id": run_id, "status": "MR_READY"}
    if run.get("mr_iid") and GL.enabled:
        result["mr"] = GL.mark_mr_ready(run["mr_iid"])
    STORE.clear_active_run()
    return result


@mcp.tool()
def flow_abort(run_id: str, remove_worktree: bool = False) -> dict[str, Any]:
    """中止当前受控开发流并解除主工作区防呆锁。"""
    run = STORE.get_run(run_id)
    if not run:
        return {"error": f"未知 run_id: {run_id}"}
    task = BG_TASKS.pop(run_id, None)
    if task and not task.done():
        task.cancel()
    if remove_worktree and run.get("worktree_path"):
        subprocess.run(
            ["git", "worktree", "remove", "--force", run["worktree_path"]],
            cwd=str(REPO_ROOT),
        )
    STORE.update_run(run_id, status="ABORTED")
    return {"run_id": run_id, "status": "ABORTED"}


if __name__ == "__main__":
    mcp.run()
