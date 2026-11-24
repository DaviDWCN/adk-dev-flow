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
        return {
            "status": "IDLE",
            "step": 0,
            "highlight_critic": False,
            "message": "暂无活跃任务",
            "repo_name": repo_name,
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
    return {
        "run_id": run_data["run_id"],
        "status": status,
        "step": lifecycle_step(status),
        "highlight_critic": lifecycle_highlight_critic(status),
        "issue_iid": run_data.get("issue_iid"),
        "branch": run_data.get("branch") or (f"adk/issue-{run_data.get('issue_iid')}" if run_data.get("issue_iid") else "main"),
        "worktree_path": run_data.get("worktree_path") or "",
        "attempt": run_data.get("attempt", 0),
        "mr_url": run_data.get("mr_url"),
        "last_log": (run_data.get("last_error_log") or "")[-800:],
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
