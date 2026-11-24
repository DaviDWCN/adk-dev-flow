from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure all agents use CodeBuddy's free model: hy4-flash-free
os.environ["ADK_WORKER_MODEL"] = "hy4-flash-free"
os.environ["ADK_CRITIC_MODEL"] = "hy4-flash-free"
os.environ["CODEBUDDY_MODEL"] = "hy4-flash-free"
os.environ["ADK_WORKER_PROVIDER"] = "codebuddy"
os.environ["CODEBUDDY_USE_STDIN"] = "1"
os.environ["ADK_RECOVERY_GRACE_PERIOD"] = "600.0"

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "src"))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from adk_dev_flow.dashboard import resolve_node_status_info
from adk_dev_flow.mcp_server import create_mcp_server
from adk_dev_flow.models import AtomicTask, SpecContract


TEST_CODE = """from __future__ import annotations

import pytest
from adk_dev_flow.worktree_utils import format_byte_size, truncate_log_safely


def test_format_byte_size():
    assert format_byte_size(500) == "500 B"
    assert format_byte_size(1024) == "1.0 KB"
    assert format_byte_size(2048) == "2.0 KB"
    assert format_byte_size(1024 * 1024 * 5) == "5.0 MB"


def test_truncate_log_safely():
    assert truncate_log_safely("hello world", max_chars=20) == "hello world"
    long_text = "a" * 100
    res = truncate_log_safely(long_text, max_chars=20, suffix="... [tail]")
    assert len(res) == 20
    assert res.endswith("... [tail]")
    assert res == "a" * 10 + "... [tail]"
"""

DESIGN_DOC = """# 实用工具架构设计方案 (Utilities Architecture)

## 1. 需求与目标
为 `src/adk_dev_flow/worktree_utils.py` 提供安全日志截断与字节格式化辅助函数，防止长日志溢出并提供清晰易读的文件大小展示。

## 2. 接口契约
- `format_byte_size(size_bytes: int) -> str`:
  - `< 1024`: 格式化为 `f"{size_bytes} B"`
  - `< 1024**2`: 格式化为 `f"{size_bytes / 1024:.1f} KB"`
  - `< 1024**3`: 格式化为 `f"{size_bytes / (1024**2):.1f} MB"`
  - `>= 1024**3`: 格式化为 `f"{size_bytes / (1024**3):.1f} GB"`
- `truncate_log_safely(text: str, max_chars: int = 500, suffix: str = "... [truncated]") -> str`:
  - 若 `len(text) <= max_chars`: 直接返回原字符串
  - 若 `max_chars <= len(suffix)`: 返回 `text[:max_chars]`
  - 否则截取 `text[:max_chars - len(suffix)] + suffix`，严格保证返回值长度 `<= max_chars`
"""


async def main():
    print("=" * 65)
    print("🚀 启动 ADK Dev Flow 确定性研发演示任务")
    print("🤖 全链路 Agent 模型配置: CodeBuddy [hy4-flash-free]")
    print("📺 像素工坊大屏地址: http://127.0.0.1:8080/")
    print("=" * 65)

    _, service = create_mcp_server(repo_root=repo_root)

    # 1. flow_init (工位 1 · 架构设计工位)
    print("\n[阶段 1/8] 📋 初始化流水线 (flow_init)...")
    init_res = await service.flow_init(
        requirement="为 worktree_utils 添加安全日志截断 truncate_log_safely 与字节大小格式化 format_byte_size 工具函数",
        repo_root=str(repo_root),
    )
    run_id = init_res["run_id"]
    print(f"  ✓ 流水线已创建 | run_id = {run_id}")
    print(f"  ✓ 当前状态: {init_res['status']}")

    # 2. flow_analyze_impact (工位 1 · AST 影响面推导)
    print("\n[阶段 2/8] 📐 AST 静态影响面分析 (flow_analyze_impact)...")
    impact_res = service.flow_analyze_impact(
        target_paths=["src/adk_dev_flow/worktree_utils.py"],
        include_skeletons=True,
    )
    print(f"  ✓ 影响生产文件: {impact_res.get('impacted_production_files', [])}")
    print(f"  ✓ 建议白名单: {impact_res.get('suggested_allowed_paths', [])}")

    # 3. flow_preflight_test (工位 2 · 真实基线红灯预检)
    print("\n[阶段 3/8] 🔴 基线 TDD 红灯预检 (flow_preflight_test)...")
    test_path = "tests/test_demo_feature.py"
    preflight_res = service.flow_preflight_test(
        test_path=test_path,
        test_code=TEST_CODE,
        run_id=run_id,
    )
    print(f"  ✓ 红灯预检结果: ok={preflight_res.get('ok')}, exit_code={preflight_res.get('exit_code')}")
    print(f"  ✓ 纯净语义断言失败: {preflight_res.get('is_pure_semantic_failure')}")
    if not preflight_res.get("ok"):
        print(f"  ❌ 预检未通过: {preflight_res.get('error')}")
        return

    # 4. flow_lock_spec (工位 4 · 关键 HITL 契约锁定)
    print("\n[阶段 4/8] ✍️ 冻结锁定任务契约 (flow_lock_spec)...")
    spec = SpecContract(
        title="worktree_utils 实用工具函数扩充",
        summary="在 worktree_utils.py 中实现 format_byte_size 和 truncate_log_safely，提供健壮的日志截断与大小格式化能力。",
        target_branch="main",
        allowed_paths=["src/adk_dev_flow/worktree_utils.py"],
        acceptance_test_path=test_path,
        acceptance_test_code=TEST_CODE,
        require_tdd_red=True,
        require_mutation_gate=False,
        tasks=[
            AtomicTask(
                id="T1",
                title="实现字节格式化与日志截断",
                description="在 worktree_utils.py 中替换桩函数，实现 format_byte_size 与 truncate_log_safely",
                target_files=["src/adk_dev_flow/worktree_utils.py"],
                target_symbols=["format_byte_size", "truncate_log_safely"],
            )
        ],
        design_doc=DESIGN_DOC,
    )
    lock_res = service.flow_lock_spec(run_id=run_id, spec=spec)
    print(f"  ✓ 契约已物理锁定 | status = {lock_res.get('status')}")
    print("  ✓ 大屏状态现已同步显示为: ✍️ 契约已锁定 · 等待派发")

    # 5. flow_dispatch (工位 5-8 · 派发沙箱后台流水线)
    print("\n[阶段 5/8] 🔨 派发后台沙箱工兵并启动 Graph 流水线 (flow_dispatch)...")
    dispatch_res = await service.flow_dispatch(run_id=run_id)
    print(f"  ✓ 任务派发成功: {dispatch_res}")
    print(f"  ✓ 后台工作树与 CodeBuddy Worker [hy4-flash-free] 已激活")

    # 6. 实时追踪流水线执行与大屏同步
    print("\n[阶段 6/8] 📡 实时轮询追踪状态（大屏 http://127.0.0.1:8080/ 同步演进）...")
    last_status = None
    start_time = time.time()
    while True:
        status_data = service.flow_status(run_id)
        current_status = status_data.get("status")
        attempt = status_data.get("attempt", 0)
        node_info = resolve_node_status_info(current_status, attempt=attempt)

        if current_status != last_status:
            print(f"\n  👉 状态跃迁: [{current_status}] | {node_info['stage_name']}")
            print(f"     • 活跃角色: {node_info['actor_role']}")
            print(f"     • 当前动作: {node_info['current_action']}")
            print(f"     • 等待事项: {node_info['waiting_for']}")
            print(f"     • 工位定位: {node_info['station_name']}")
            last_status = current_status

        if current_status in ("MR_READY", "ESCALATED", "ABORTED"):
            print(f"\n🏁 流水线运行结束: 最终状态 = {current_status}")
            if current_status == "MR_READY":
                print(f"  🎉 恭喜！成果交付就绪！")
                print(f"  📄 MR/PR 链接: {status_data.get('mr_url')}")
            else:
                print(f"  ⚠️ 熔断或中止原因: {status_data.get('last_log', '')[-300:]}")
            break

        if time.time() - start_time > 360:
            print("  ⚠️ 超时退出轮询")
            break

        await asyncio.sleep(2.5)

    print("\n" + "=" * 65)
    print("✨ Demo 任务全生命周期执行完毕")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
