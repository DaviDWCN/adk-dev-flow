from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any, AsyncGenerator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from adk_dev_flow.state_store import StateStore, lifecycle_highlight_critic, lifecycle_step


def resolve_node_status_info(
    status: str,
    attempt: int = 0,
    last_log: str = "",
    mr_url: str | None = None,
) -> dict[str, Any]:
    """Resolve realistic flow node descriptor, active actor, and human-readable progress state."""
    att = attempt or 1
    info_map: dict[str, dict[str, Any]] = {
        "IDLE": {
            "node_key": "IDLE",
            "station_id": 0,
            "station_name": "工位 0 · 待命区",
            "stage_name": "系统待命 · STANDBY",
            "actor_role": "系统调度器",
            "actor_name": "Standby Dispatcher",
            "badge_text": "⚡ 待命待发",
            "badge_style": "bg-slate-100 text-slate-600 border-slate-300",
            "current_action": "流水线处于空闲待命状态，无活跃执行任务",
            "waiting_for": "等待派发新任务 (flow_init / flow_dispatch)",
            "is_running": False,
            "is_waiting_review": False,
        },
        "SPEC_DRAFTING": {
            "node_key": "SPEC",
            "station_id": 1,
            "station_name": "工位 1 · 架构设计工位",
            "stage_name": "步骤 1/8 · 架构蓝图与任务拆分",
            "actor_role": "架构师 AI (Architect Agent)",
            "actor_name": "Architect Agent",
            "badge_text": "📐 架构师方案起草中",
            "badge_style": "bg-blue-50 text-blue-700 border-blue-300",
            "current_action": "架构师 AI 正在分析代码依赖与 AST 影响面，起草设计文档与原子任务清单",
            "waiting_for": "等待架构设计文档生成",
            "is_running": True,
            "is_waiting_review": False,
        },
        "SPEC_DESIGNED": {
            "node_key": "SPEC",
            "station_id": 2,
            "station_name": "工位 2 · 验收测试实验室",
            "stage_name": "步骤 2/8 · 验收测试编写与变异验真",
            "actor_role": "测试工程师 AI (QA Tester Agent)",
            "actor_name": "QA Tester Agent",
            "badge_text": "🧪 验收测试编写与验真中",
            "badge_style": "bg-emerald-50 text-emerald-700 border-emerald-300",
            "current_action": "测试 AI 正在编写验收用例，执行真实红灯预检与 AST 变异测试击杀分析 (要求 ≥85%)",
            "waiting_for": "等待测试编写与变异门禁判定",
            "is_running": True,
            "is_waiting_review": False,
        },
        "TEST_GENERATED": {
            "node_key": "SPEC",
            "station_id": 2,
            "station_name": "工位 2 · 验收测试实验室",
            "stage_name": "步骤 2/8 · AST 变异测试客观门禁",
            "actor_role": "变异门禁分析引擎 (Mutation Gate Engine)",
            "actor_name": "Mutation Analyzer",
            "badge_text": "🧬 变异击杀率门禁评估中",
            "badge_style": "bg-emerald-50 text-emerald-700 border-emerald-300",
            "current_action": "用例已生成，变异分析引擎正在对被测函数施加语法变异体，严格验证断言击杀有效性",
            "waiting_for": "等待变异击杀率达到 85% 门禁标准",
            "is_running": True,
            "is_waiting_review": False,
        },
        "PRE_CRITIC_REVIEWED": {
            "node_key": "CRITIC",
            "station_id": 3,
            "station_name": "工位 3 · 前置双审台",
            "stage_name": "步骤 3/8 · 前置规范双审",
            "actor_role": "独立审查专家 (Critic Agent)",
            "actor_name": "Critic Reviewer",
            "badge_text": "🧐 前置规范审查中 (等待审批)",
            "badge_style": "bg-cyan-50 text-cyan-700 border-cyan-300",
            "current_action": "第三视角审查员正在双审设计与用例一致性，校验架构规范与公共签名保护",
            "waiting_for": "等待前置双审放行",
            "is_running": False,
            "is_waiting_review": True,
        },
        "SPEC_LOCKED": {
            "node_key": "SPEC",
            "station_id": 4,
            "station_name": "工位 4 · 签字锁定期",
            "stage_name": "步骤 4/8 · 关键 HITL 契约锁定",
            "actor_role": "人工审核员 / 调度器 (Human-in-the-Loop)",
            "actor_name": "HITL Reviewer",
            "badge_text": "✍️ 契约已锁定 · 等待派发",
            "badge_style": "bg-amber-50 text-amber-700 border-amber-300 ring-2 ring-amber-400/40 animate-pulse",
            "current_action": "任务契约与测试用例已锁定签名，环境准备就绪，等待派发沙箱编码施工",
            "waiting_for": "等待调用 flow_dispatch 启动工兵沙箱开发",
            "is_running": False,
            "is_waiting_review": True,
        },
        "RUNNING": {
            "node_key": "WORK",
            "station_id": 5,
            "station_name": "工位 5 · 沙箱编码施工区",
            "stage_name": "步骤 5/8 · 开发工兵沙箱施工",
            "actor_role": "开发工兵 Agent (Worker Agent)",
            "actor_name": "Builder Agent",
            "badge_text": "🔨 开发工兵沙箱编码施工中",
            "badge_style": "bg-amber-50 text-amber-800 border-amber-300 ring-2 ring-amber-400/30",
            "current_action": "开发工兵 Agent 正在独立沙箱 Worktree 中受限编码施工，目标全量跑通验收测试",
            "waiting_for": "等待工兵编码完成提交",
            "is_running": True,
            "is_waiting_review": False,
        },
        "VERIFYING": {
            "node_key": "TEST",
            "station_id": 6,
            "station_name": "工位 6 · 自动化测试跑道",
            "stage_name": "步骤 6/8 · 自动化测试真机跑测",
            "actor_role": "自动化测试跑道引擎 (Automated Test Runner)",
            "actor_name": "Test Runner",
            "badge_text": "⚙️ 自动化测试真机跑测中",
            "badge_style": "bg-purple-50 text-purple-700 border-purple-300 ring-2 ring-purple-400/30",
            "current_action": "机械跑道正在执行测试防篡改审计、Ruff 规范静态扫描与全量 Pytest 真机跑测",
            "waiting_for": "等待自动化测试全绿通过",
            "is_running": True,
            "is_waiting_review": False,
        },
        "REPAIRING": {
            "node_key": "WORK",
            "station_id": 7,
            "station_name": "工位 7 · 自愈回流环",
            "stage_name": "步骤 7/8 · 错误指纹定向自愈",
            "actor_role": "自愈修复工兵 (Self-Healing Worker)",
            "actor_name": "Self-Healing Agent",
            "badge_text": f"🔄 错误指纹定向自愈修复中 (第 {att}/3 轮)",
            "badge_style": "bg-orange-50 text-orange-700 border-orange-300 ring-2 ring-orange-400/40",
            "current_action": f"测试未通过，系统已提取错误指纹回路送回工兵工位进行第 {att}/3 轮定向修复",
            "waiting_for": "等待工兵修复并重新送测",
            "is_running": True,
            "is_waiting_review": False,
        },
        "REVIEWING": {
            "node_key": "MR",
            "station_id": 8,
            "station_name": "工位 8 · 成果交付代码库",
            "stage_name": "步骤 8/8 · AST 语义门禁审核",
            "actor_role": "AST 语义硬门禁引擎 (AST Gatekeeper)",
            "actor_name": "AST Gatekeeper",
            "badge_text": "🛡️ AST 语义门禁审核中",
            "badge_style": "bg-cyan-50 text-cyan-700 border-cyan-300",
            "current_action": "正在执行 AST 静态语法树深度对比，严格排查破坏性公共签名变更与危险动态代码",
            "waiting_for": "等待 AST 语义门禁放行",
            "is_running": True,
            "is_waiting_review": False,
        },
        "MR_READY": {
            "node_key": "MR",
            "station_id": 8,
            "station_name": "工位 8 · 成果交付代码库",
            "stage_name": "步骤 8/8 · 成果交付代码库",
            "actor_role": "代码仓库评审人 (Code Reviewer)",
            "actor_name": "Repo Delivery Gate",
            "badge_text": "🎉 成果交付就绪 · 等待合并评审",
            "badge_style": "bg-emerald-50 text-emerald-700 border-emerald-300 ring-2 ring-emerald-400/40",
            "current_action": "全套测试验证通过，代码已安全存入仓库，已生成 PR/MR 并附带全套存证，等待人工合并评审",
            "waiting_for": "等待人工在 GitLab/GitHub 上评审并合并 PR/MR",
            "is_running": False,
            "is_waiting_review": True,
        },
        "ESCALATED": {
            "node_key": "ESCALATE",
            "station_id": 9,
            "station_name": "工位 9 · 异常熔断安全区",
            "stage_name": "步骤 9 · 异常熔断跳闸保护",
            "actor_role": "物理熔断保护器 (Circuit Breaker)",
            "actor_name": "Circuit Breaker",
            "badge_text": "🚨 异常跳闸 · 等待人工排障",
            "badge_style": "bg-rose-50 text-rose-700 border-rose-300 ring-2 ring-rose-500/50 animate-pulse",
            "current_action": "自愈重试超限或出现严重异常，触发物理熔断机制强制停机，保护工作现场未受污染",
            "waiting_for": "等待人工排障复位 (调用 flow_resume 或 flow_abort)",
            "is_running": False,
            "is_waiting_review": True,
        },
        "ABORTED": {
            "node_key": "ABORT",
            "station_id": 0,
            "station_name": "工位 0 · 终止归档区",
            "stage_name": "任务已中止 · ABORTED",
            "actor_role": "操作员 (Operator)",
            "actor_name": "Operator",
            "badge_text": "⛔ 任务已中止",
            "badge_style": "bg-slate-100 text-slate-600 border-slate-300",
            "current_action": "该任务已被人工终止或异常清除，现场已妥善回收，主干零污染",
            "waiting_for": "等待派发新任务",
            "is_running": False,
            "is_waiting_review": False,
        },
    }
    return info_map.get(
        status,
        {
            "node_key": "IDLE",
            "station_id": 0,
            "station_name": "工位 0 · 待命区",
            "stage_name": f"状态: {status}",
            "actor_role": "系统",
            "actor_name": "System",
            "badge_text": f"状态: {status}",
            "badge_style": "bg-slate-100 text-slate-600 border-slate-300",
            "current_action": last_log or f"当前处于状态 {status}",
            "waiting_for": "无",
            "is_running": False,
            "is_waiting_review": False,
        },
    )


def get_dashboard_html(initial_run_id: str | None = None) -> str:
    """Read the cartoon worker HTML dashboard template and inject initial run ID if present."""
    html = resources.files("adk_dev_flow.templates").joinpath("dashboard.html").read_text(encoding="utf-8")
    if initial_run_id:
        # Pre-seed currentRunId in the inline script for instant rendering
        html = html.replace(
            "let currentRunId = null;",
            f'let currentRunId = "{initial_run_id}";',
        )
    return html


def _build_run_payload(run_data: dict[str, Any] | None, repo_name: str) -> dict[str, Any]:
    if not run_data:
        idle_info = resolve_node_status_info("IDLE")
        return {
            "status": "IDLE",
            "step": 0,
            "highlight_critic": False,
            "message": "暂无活跃任务",
            "repo_name": repo_name,
            "node_info": idle_info,
            "current_step_name": idle_info["stage_name"],
            "active_actor": idle_info["actor_role"],
            "current_action": idle_info["current_action"],
            "waiting_for": idle_info["waiting_for"],
            "badge_text": idle_info["badge_text"],
            "badge_style": idle_info["badge_style"],
        }

    spec: dict[str, Any] = {}
    if run_data.get("spec_json"):
        try:
            spec = json.loads(run_data["spec_json"])
        except Exception:
            spec = {}

    review: dict[str, Any] | None = None
    if run_data.get("review_json"):
        try:
            review = json.loads(run_data["review_json"])
        except Exception:
            review = None

    status = str(run_data["status"])
    attempt = int(run_data.get("attempt") or 0)
    last_log = (run_data.get("last_error_log") or "")[-800:]
    mr_url = run_data.get("mr_url")
    node_info = resolve_node_status_info(status, attempt=attempt, last_log=last_log, mr_url=mr_url)

    return {
        "run_id": run_data["run_id"],
        "status": status,
        "step": lifecycle_step(status),
        "highlight_critic": lifecycle_highlight_critic(status),
        "node_info": node_info,
        "current_step_name": node_info["stage_name"],
        "active_actor": node_info["actor_role"],
        "current_action": node_info["current_action"],
        "waiting_for": node_info["waiting_for"],
        "badge_text": node_info["badge_text"],
        "badge_style": node_info["badge_style"],
        "issue_iid": run_data.get("issue_iid"),
        "branch": run_data.get("branch") or (f"adk/issue-{run_data.get('issue_iid')}" if run_data.get("issue_iid") else "main"),
        "worktree_path": run_data.get("worktree_path") or "",
        "attempt": attempt,
        "mr_url": mr_url,
        "last_log": last_log,
        "updated_at": run_data.get("updated_at"),
        "repo_name": repo_name,
        "allowed_paths": spec.get("allowed_paths", []),
        "locked_test": spec.get("acceptance_test_path", ""),
        "target_branch": spec.get("target_branch", "main"),
        "spec_title": spec.get("title", ""),
        "require_tdd_red": spec.get("require_tdd_red", True),
        "require_mutation_gate": spec.get("require_mutation_gate", True),
        "mutation_report": spec.get("mutation_report"),
        "design_doc": spec.get("design_doc", ""),
        "red_evidence": spec.get("red_evidence"),
        "pre_critic_report": spec.get("pre_critic_report"),
        "impact_analysis": spec.get("impact_analysis"),
        "review": review,
    }


def create_dashboard_app(
    repo_root: Path | None = None,
    store: StateStore | None = None,
) -> Starlette:
    """Create a Starlette ASGI application providing the dashboard and real-time SSE stream."""
    resolved_root = (repo_root or Path(os.environ.get("REPO_ROOT", os.getcwd()))).resolve()
    resolved_store = store or StateStore(resolved_root)

    async def handle_index(request: Request) -> HTMLResponse:
        run_id = request.query_params.get("run_id")
        return HTMLResponse(get_dashboard_html(initial_run_id=run_id))

    async def handle_status(request: Request) -> JSONResponse:
        run_id = request.query_params.get("run_id")
        run_data: dict[str, Any] | None = None
        if run_id:
            run_data = resolved_store.get_run(run_id)
        else:
            active_runs = resolved_store.get_active_runs()
            if active_runs:
                run_data = active_runs[0]
            else:
                recent = resolved_store.list_runs(limit=1)
                run_data = recent[0] if recent else None

        payload = _build_run_payload(run_data, resolved_root.name)
        return JSONResponse(payload, status_code=200)

    async def handle_runs(request: Request) -> JSONResponse:
        runs = resolved_store.list_runs(limit=25)
        return JSONResponse(
            {
                "runs": [
                    {
                        "run_id": r["run_id"],
                        "status": r["status"],
                        "branch": r.get("branch"),
                        "attempt": r.get("attempt", 0),
                        "updated_at": r.get("updated_at"),
                    }
                    for r in runs
                ]
            }
        )

    async def event_generator(run_id: str | None) -> AsyncGenerator[str, None]:
        last_sig: str | None = None
        while True:
            try:
                run_data: dict[str, Any] | None = None
                if run_id:
                    run_data = resolved_store.get_run(run_id)
                else:
                    active = resolved_store.get_active_runs()
                    if active:
                        run_data = active[0]
                    else:
                        recent = resolved_store.list_runs(limit=1)
                        run_data = recent[0] if recent else None

                payload = _build_run_payload(run_data, resolved_root.name)
                sig = json.dumps(payload, sort_keys=True)
                if sig != last_sig:
                    last_sig = sig
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                else:
                    # Keep-alive comment for SSE stream
                    yield ": ping\n\n"

                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    async def handle_events(request: Request) -> StreamingResponse:
        run_id = request.query_params.get("run_id")
        return StreamingResponse(
            event_generator(run_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    routes = [
        Route("/", endpoint=handle_index, methods=["GET"]),
        Route("/dashboard", endpoint=handle_index, methods=["GET"]),
        Route("/api/status", endpoint=handle_status, methods=["GET"]),
        Route("/api/runs", endpoint=handle_runs, methods=["GET"]),
        Route("/api/events", endpoint=handle_events, methods=["GET"]),
    ]

    return Starlette(routes=routes)


def find_available_port(host: str = "127.0.0.1", start_port: int = 8080, max_attempts: int = 50) -> int:
    """Find the first available TCP port starting from start_port."""
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start_port


def run_dashboard_server(
    repo_root: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = False,
    auto_port: bool = True,
) -> int:
    """Run the dashboard HTTP & SSE server with uvicorn, auto-resolving ports if occupied."""
    import uvicorn

    actual_port = find_available_port(host=host, start_port=port) if auto_port else port
    app = create_dashboard_app(repo_root=repo_root)

    if open_browser:
        target_url = f"http://{host}:{actual_port}/"
        asyncio.get_event_loop().call_later(0.5, lambda: webbrowser.open(target_url))

    if actual_port != port:
        print(f"⚠️ 端口 {port} 已被占用，已自动切换至空闲端口: http://{host}:{actual_port}/")

    uvicorn.run(app, host=host, port=actual_port, log_level="info")
    return actual_port


def spawn_detached_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    repo_root: Path | None = None,
) -> None:
    """Spawn the dashboard server as a detached process that survives the caller's exit."""
    resolved = (repo_root or Path(os.environ.get("REPO_ROOT", os.getcwd()))).resolve()
    cmd = [
        sys.executable,
        "-m",
        "adk_dev_flow.dashboard",
        "--host",
        host,
        "--port",
        str(port),
        "--repo",
        str(resolved),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adk-dev-flow-dashboard",
        description="Launch the animated cartoon worker status dashboard for adk-dev-flow.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port number to bind (default: 8080)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="Path to repository root (default: current directory)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Automatically open the dashboard in default browser",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional initial run_id to monitor",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Spawn the dashboard server as a detached background process and exit immediately",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve()
    if args.detach:
        spawn_detached_server(host=args.host, port=args.port, repo_root=root)
        print(f"🛰️ 已后台启动看板服务: http://{args.host}:{args.port}/")
        return 0

    print(f"🚀 启动 ADK 像素工坊实时监控看板: http://{args.host}:{args.port}/")
    try:
        run_dashboard_server(
            repo_root=root,
            host=args.host,
            port=args.port,
            open_browser=args.open,
        )
    except KeyboardInterrupt:
        print("\n👋 像素工坊看板已安全停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
