from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure repo root is in python path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adk_dev_flow.forge_client import NullGitForge
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import execute_workflow_run


async def run_demo() -> None:
    demo_root = Path("d:/workspace/demo-math-service").resolve()
    spec_file = demo_root / ".adk" / "specs" / "feat-token-bucket.md"

    # Configure free model hy4-flash-free
    os.environ["ADK_WORKER_MODEL"] = "hy4-flash-free"
    os.environ["ADK_CRITIC_MODEL"] = "hy4-flash-free"
    os.environ["ADK_WORKER_PROVIDER"] = "codebuddy"
    os.environ["REPO_ROOT"] = str(demo_root)
    os.environ["GIT_FORGE_PROVIDER"] = "none"

    store = StateStore(demo_root)
    forge = NullGitForge()
    service = FlowService(demo_root, store, forge)

    print("=" * 60)
    print("🚀 [ADK Dev Flow] 启动 Demo 验证流程 (Model: hy4-flash-free)")
    print(f"📁 目标仓库: {demo_root}")
    print("=" * 60)

    # 1. Flow Init
    init_res = await service.flow_init(requirement="在 math_service 中实现 TokenBucketRateLimiter")
    run_id = init_res["run_id"]
    print(f"✨ 步骤 1: flow_init 初始化成功 -> run_id: {run_id}")

    # 2. Lock Spec from OKF Markdown
    print(f"📐 步骤 2: 读取并锁定 OKF 规约: {spec_file.name}")
    lock_res = service.flow_lock_spec_from_file(run_id, str(spec_file))
    if "error" in lock_res:
        print(f"❌ 锁定失败: {lock_res['error']}")
        return
    print(f"🔒 Spec 锁定成功，验收测试: {lock_res.get('locked_test')}")

    # 3. Transition to RUNNING
    store.transition_run(run_id, expected_statuses=("SPEC_LOCKED",), new_status="RUNNING")

    # 4. Execute ADK 2.x Workflow
    print("🔨 步骤 3: 调度 Google ADK 2.x 工作流引擎...")
    await execute_workflow_run(demo_root, store, forge, run_id)

    # 5. Check final status
    final_status = service.flow_status(run_id)
    print("=" * 60)
    print(f"🏁 流水线执行结束，最终状态: {final_status['status']}")
    print(f"🔄 修复轮次: {final_status.get('attempt', 0)}")
    if final_status.get("review"):
        print(f"🧐 Critic 审查结论: {final_status['review']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_demo())
