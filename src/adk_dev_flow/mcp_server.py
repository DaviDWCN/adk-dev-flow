from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from adk_dev_flow.dashboard import get_dashboard_html
from adk_dev_flow.forge_client import BaseGitForge, GitLabClient, get_git_forge
from adk_dev_flow.models import SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.workflow import execute_workflow_run


class FlowService:
    """Encapsulates MCP tool business logic so it can be cleanly instantiated and unit-tested."""

    def __init__(
        self,
        repo_root: Path,
        store: StateStore,
        gl: BaseGitForge | None = None,
        forge: BaseGitForge | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.store = store
        self.gl: BaseGitForge = forge or gl or get_git_forge(self.repo_root)
        self.forge: BaseGitForge = self.gl
        self.bg_tasks: dict[str, asyncio.Task[None]] = {}
        # Mark any in-flight runs left over from a prior server restart as ESCALATED.
        self.recovered_runs = self.store.recover_orphaned_runs()

    def _register_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        self.bg_tasks[run_id] = task

        def _cleanup(_t: asyncio.Task[None]) -> None:
            if self.bg_tasks.get(run_id) is _t:
                self.bg_tasks.pop(run_id, None)

        task.add_done_callback(_cleanup)

    async def flow_init(self, requirement: str, issue_iid: int | None = None) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:12]
        issue_info: dict[str, Any] | None = None
        forge_warning: str | None = None
        if issue_iid is not None and self.gl.enabled:
            try:
                issue_info = await asyncio.to_thread(self.gl.get_issue, issue_iid)
            except Exception as exc:
                forge_warning = f"拉取 {self.gl.platform_name.upper()} Issue #{issue_iid} 失败，已继续初始化: {exc}"

        self.store.create_run(run_id=run_id, issue_iid=issue_iid)
        result: dict[str, Any] = {
            "run_id": run_id,
            "status": "SPEC_DRAFTING",
            "requirement": requirement,
            "issue": issue_info,
            "gitlab_issue": issue_info,
            "next_step": (
                "请结合仓库结构起草 SpecContract（含完整可运行的 pytest 验收测试源码），"
                "向用户展示并获得明确确认后再调用 flow_lock_spec 或 flow_lock_spec_from_file。"
            ),
        }
        if forge_warning:
            result["forge_warning"] = forge_warning
            result["gitlab_warning"] = forge_warning
        return result

    def flow_lock_spec(self, run_id: str, spec: SpecContract) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}

        ok = self.store.transition_run(
            run_id,
            expected_statuses=("SPEC_DRAFTING",),
            new_status="SPEC_LOCKED",
            spec_json=spec.model_dump_json(),
        )
        if not ok:
            return {
                "error": f"当前状态不可锁定 Spec（需 SPEC_DRAFTING，实际为 {run['status']}）"
            }

        ast_impact: dict[str, Any] | None = None
        try:
            from adk_dev_flow.ast_graph import CodeGraph

            cg = CodeGraph(self.repo_root)
            report = cg.analyze_impact(spec.allowed_paths)
            ast_impact = {
                "impacted_production_files": report.impacted_production_files,
                "impacted_test_files": report.impacted_test_files,
                "suggested_allowed_paths": report.suggested_allowed_paths,
                "summary": report.summary,
            }
        except Exception:
            ast_impact = None

        res: dict[str, Any] = {
            "run_id": run_id,
            "status": "SPEC_LOCKED",
            "locked_test": spec.acceptance_test_path,
            "allowed_paths": spec.allowed_paths,
            "next_step": "Spec 已冻结并通过静态校验，可调用 flow_dispatch 触发 ADK 后台隔离执行与验收。",
        }
        if ast_impact:
            res["ast_impact"] = ast_impact
        return res

    def flow_analyze_impact(self, target_paths: list[str]) -> dict[str, Any]:
        from adk_dev_flow.ast_graph import CodeGraph

        cg = CodeGraph(self.repo_root)
        report = cg.analyze_impact(target_paths)
        return report.to_dict()

    def flow_lock_spec_from_file(self, run_id: str, spec_file_path: str) -> dict[str, Any]:
        p = Path(spec_file_path)
        if not p.is_absolute():
            p = (self.repo_root / p).resolve()
        if not p.is_file():
            return {"error": f"规约文件未找到: {spec_file_path} (resolved: {p})"}
        try:
            spec = SpecContract.from_okf_markdown(p)
        except Exception as exc:
            return {"error": f"OKF 规约文件解析失败: {exc}"}
        return self.flow_lock_spec(run_id=run_id, spec=spec)

    async def flow_dispatch(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}

        # Atomic CAS transition prevents duplicate concurrent launches.
        ok = self.store.transition_run(
            run_id,
            expected_statuses=("SPEC_LOCKED",),
            new_status="RUNNING",
        )
        if not ok:
            return {
                "error": f"当前状态不可派发（需 SPEC_LOCKED，实际为 {run['status']}）"
            }

        task = asyncio.create_task(
            execute_workflow_run(self.repo_root, self.store, self.gl, run_id)
        )
        self._register_task(run_id, task)
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "message": "ADK 确定性流水线已在后台隔离 Worktree 启动，请调用 flow_status 查询进度。",
        }

    async def flow_resume(self, run_id: str, instruction: str) -> dict[str, Any]:
        hint = instruction.strip()
        if not hint:
            return {"error": "请提供非空的补充指令 (instruction) 后再恢复执行。"}

        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}
        if not run.get("spec_json"):
            return {"error": f"run_id={run_id} 尚未锁定 Spec，无法恢复执行。"}

        ok = self.store.transition_run(
            run_id,
            expected_statuses=("ESCALATED",),
            new_status="RUNNING",
            attempt=0,
            last_error_sig="",
            human_hint=hint,
        )
        if not ok:
            return {
                "error": f"当前状态不可恢复（仅 ESCALATED 可调用 flow_resume，实际为 {run['status']}）"
            }

        task = asyncio.create_task(
            execute_workflow_run(
                self.repo_root, self.store, self.gl, run_id, human_hint=hint
            )
        )
        self._register_task(run_id, task)
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "message": "已携人工补充指令在原隔离 Worktree 恢复流水线，请调用 flow_status 查询进度。",
        }

    def flow_status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "issue_iid": run["issue_iid"],
            "branch": run["branch"],
            "worktree_path": run["worktree_path"],
            "base_sha": run.get("base_sha"),
            "attempt": run["attempt"],
            "mr_iid": run["mr_iid"],
            "mr_url": run["mr_url"],
            "review": json.loads(run["review_json"]) if run["review_json"] else None,
            "human_hint": run.get("human_hint"),
            "last_error_log": (run["last_error_log"] or "")[-1500:],
            "updated_at": run["updated_at"],
        }

    def flow_list(self, limit: int = 10) -> dict[str, Any]:
        runs = self.store.list_runs(limit=limit)
        items = [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "issue_iid": r["issue_iid"],
                "branch": r["branch"],
                "attempt": r["attempt"],
                "mr_url": r["mr_url"],
                "updated_at": r["updated_at"],
            }
            for r in runs
        ]
        return {"runs": items, "count": len(items)}

    async def _remove_worktree(self, worktree_path: str) -> str | None:
        res = await run_subprocess_safe(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=self.repo_root,
            timeout_sec=30.0,
        )
        await run_subprocess_safe(
            ["git", "worktree", "prune"],
            cwd=self.repo_root,
            timeout_sec=15.0,
        )
        if res.returncode != 0:
            return res.stderr.strip() or f"git worktree remove exited {res.returncode}"
        return None

    async def flow_finalize(self, run_id: str, remove_worktree: bool = True) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}
        if run["status"] != "MR_READY":
            return {
                "error": f"当前状态不可 Finalize（需 MR_READY，实际为 {run['status']}）"
            }

        result: dict[str, Any] = {"run_id": run_id, "status": "MR_READY"}
        if run.get("mr_iid") and self.gl.enabled:
            try:
                result["mr"] = await asyncio.to_thread(self.gl.mark_mr_ready, run["mr_iid"])
            except Exception as exc:
                return {"error": f"移除 {self.gl.platform_name.upper()} PR/MR Draft 标记失败: {exc}"}

        if remove_worktree and run.get("worktree_path"):
            wt_err = await self._remove_worktree(run["worktree_path"])
            result["worktree_removed"] = wt_err is None
            if wt_err:
                result["worktree_warning"] = wt_err

        self.store._sync_active_marker()
        return result

    async def flow_abort(self, run_id: str, remove_worktree: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}

        task = self.bg_tasks.pop(run_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        wt_warning: str | None = None
        if remove_worktree and run.get("worktree_path"):
            wt_warning = await self._remove_worktree(run["worktree_path"])

        self.store.update_run(run_id, status="ABORTED")
        result: dict[str, Any] = {"run_id": run_id, "status": "ABORTED"}
        if wt_warning:
            result["worktree_warning"] = wt_warning
        return result

    def flow_active_runs(self) -> dict[str, Any]:
        runs = self.store.get_active_runs()
        items = [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "issue_iid": r["issue_iid"],
                "branch": r["branch"],
                "worktree_path": r["worktree_path"],
                "attempt": r["attempt"],
                "updated_at": r["updated_at"],
            }
            for r in runs
        ]
        return {"active_runs": items, "count": len(items)}

    def flow_dashboard(self, run_id: str | None = None) -> dict[str, Any]:
        """获取或生成用于像素工坊动画看板的运行态数据。"""
        status_info: dict[str, Any]
        if run_id:
            status_info = self.flow_status(run_id)
        else:
            active = self.store.get_active_runs()
            if active:
                status_info = self.flow_status(active[0]["run_id"])
            else:
                recent = self.store.list_runs(limit=1)
                status_info = (
                    self.flow_status(recent[0]["run_id"])
                    if recent
                    else {"status": "IDLE", "message": "暂无活跃任务"}
                )

        return {
            "content": (
                f"ADK 像素工坊看板已就绪。当前状态: {status_info.get('status', 'IDLE')}，"
                f"轮次: {status_info.get('attempt', 0)}"
            ),
            "structuredContent": status_info,
        }

    async def flow_abort_all(self, remove_worktrees: bool = False) -> dict[str, Any]:
        active_runs = self.store.get_active_runs()
        aborted = []
        for r in active_runs:
            res = await self.flow_abort(r["run_id"], remove_worktree=remove_worktrees)
            aborted.append(res)
        return {"aborted_runs": aborted, "count": len(aborted)}


def create_mcp_server(
    repo_root: Path | None = None,
    store: StateStore | None = None,
    gl: BaseGitForge | None = None,
    forge: BaseGitForge | None = None,
) -> tuple[FastMCP, FlowService]:
    resolved_root = (repo_root or Path(os.environ.get("REPO_ROOT", os.getcwd()))).resolve()
    resolved_store = store or StateStore(resolved_root)
    resolved_forge = forge or gl or get_git_forge(resolved_root)
    service = FlowService(resolved_root, resolved_store, resolved_forge)

    mcp = FastMCP("adk-dev-flow")

    @mcp.tool()
    async def flow_init(requirement: str, issue_iid: int | None = None) -> dict[str, Any]:
        """初始化确定性开发流。若提供 GitLab/GitHub Issue 编号，将自动拉取详情。"""
        return await service.flow_init(requirement=requirement, issue_iid=issue_iid)

    @mcp.tool()
    def flow_lock_spec(run_id: str, spec: SpecContract) -> dict[str, Any]:
        """静态校验并冻结用户已确认的结构化 Spec 与验收测试用例。"""
        return service.flow_lock_spec(run_id=run_id, spec=spec)

    @mcp.tool()
    def flow_lock_spec_from_file(run_id: str, spec_file_path: str) -> dict[str, Any]:
        """读取 OKF (Open Knowledge Format) Markdown 规约文件并冻结锁定该运行轮次。"""
        return service.flow_lock_spec_from_file(run_id=run_id, spec_file_path=spec_file_path)

    @mcp.tool()
    def flow_analyze_impact(target_paths: list[str]) -> dict[str, Any]:
        """使用 AST 代码图谱静态分析目标代码的影响面（爆炸半径），推导关联生产代码、调用方与受影响测试用例。"""
        return service.flow_analyze_impact(target_paths=target_paths)

    @mcp.tool()
    async def flow_dispatch(run_id: str) -> dict[str, Any]:
        """在后台启动 ADK 2.x Graph Workflow（建 Worktree、隔离编码、审计与验收自愈、Critic 审查、建 MR）。"""
        return await service.flow_dispatch(run_id=run_id)

    @mcp.tool()
    async def flow_resume(run_id: str, instruction: str) -> dict[str, Any]:
        """对已熔断（ESCALATED）的流水线注入人工补充指令，并在原隔离 Worktree 中恢复执行。"""
        return await service.flow_resume(run_id=run_id, instruction=instruction)

    @mcp.tool()
    def flow_status(run_id: str) -> dict[str, Any]:
        """查询确定性开发流的实时阶段、修复轮次、报错日志及 MR 链接。"""
        return service.flow_status(run_id=run_id)

    @mcp.tool()
    def flow_list(limit: int = 10) -> dict[str, Any]:
        """列出最近的受控开发流记录（用于跨会话找回 run_id）。"""
        return service.flow_list(limit=limit)

    @mcp.tool()
    async def flow_finalize(run_id: str, remove_worktree: bool = True) -> dict[str, Any]:
        """确认无误后移除 Draft 标记，将内网 GitLab MR 置为 Ready 并可选清理本地隔离 Worktree。"""
        return await service.flow_finalize(run_id=run_id, remove_worktree=remove_worktree)

    @mcp.tool()
    async def flow_abort(run_id: str, remove_worktree: bool = False) -> dict[str, Any]:
        """中止当前受控开发流、终止后台子进程并解除主工作区防呆锁。"""
        return await service.flow_abort(run_id=run_id, remove_worktree=remove_worktree)

    @mcp.tool()
    def flow_active_runs() -> dict[str, Any]:
        """查询当前正在并发进行的所有活跃开发流（支持多任务状态监控）。"""
        return service.flow_active_runs()

    @mcp.tool()
    async def flow_abort_all(remove_worktrees: bool = False) -> dict[str, Any]:
        """一键终止当前所有进行中的受控开发流并释放所有锁与资源。"""
        return await service.flow_abort_all(remove_worktrees=remove_worktrees)

    APP_URI = "ui://adk-dev-flow/dashboard.html"

    @mcp.resource(
        APP_URI,
        name="ADK Flow Cartoon Dashboard",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"prefersBorder": False}},
    )
    def get_dashboard_resource() -> str:
        """提供用于 Antigravity 侧边栏/内联渲染的像素工坊看板 HTML 资源。"""
        return get_dashboard_html()

    PANE_META = {
        "ui": {"resourceUri": APP_URI},
        "com.google.antigravity/ui": {
            "entrypoints": [
                {"type": "aux_pane", "title": "ADK 像素工坊看板"},
                {"type": "global", "title": "ADK 像素工坊看板"},
            ]
        },
    }

    @mcp.tool(
        name="flow_dashboard",
        title="打开 ADK 像素工坊动画看板",
        meta=PANE_META,
    )
    def flow_dashboard(run_id: str | None = None) -> dict[str, Any]:
        """打开或刷新 ADK 确定性开发流像素卡通工人工位动画看板。"""
        return service.flow_dashboard(run_id=run_id)

    return mcp, service


def main() -> None:
    mcp, _ = create_mcp_server()
    mcp.run()


if __name__ == "__main__":
    main()
