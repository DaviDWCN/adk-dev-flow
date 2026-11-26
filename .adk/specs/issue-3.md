---
type: DevSpec
title: 'fix(cpg): 污点门禁 fail-closed 并修复同名参数误报'
status: active
issue_iid: 3
target_branch: main
require_tdd_red: true
extra_dependencies: []
allowed_paths:
- src/adk_dev_flow/ast_graph.py
- src/adk_dev_flow/cpg.py
- src/adk_dev_flow/workflow.py
- tests/test_audit_taint_gate.py
- README.md
- docs/cpg-cas-architecture.md
acceptance_test_path: tests/test_audit_taint_gate.py
tasks:
- id: T1
  title: CpgBuilder 为展平语句补齐 param_pass 边
  description: 把 _visit_stmt 里通用分支的调用实参追踪提取为 _trace_call_args，并在 Assign/AnnAssign/AugAssign
    取值表达式、Return 取值、If 条件、For/While 迭代与条件上调用，使被展平语句里的实参污点可达。
  target_files:
  - src/adk_dev_flow/cpg.py
  target_symbols: []
- id: T2
  title: audit_ast_diff 污点按函数作用域隔离并 fail-closed
  description: '污点 BFS 改为按顶层函数逐个构建（每个函数自己的 param_sources 与 DFG 子图），消除跨函数同名串扰误报，同时保留真阳性（processor
    的 data 仍被指认）。DFG 段的 except Exception: pass 改为抛出 AstAuditError（带文件与阶段信息）。'
  target_files:
  - src/adk_dev_flow/ast_graph.py
  target_symbols: []
- id: T3
  title: publish_mr_node 审计段补 import 并 fail-closed
  description: 'workflow.py 补上 audit_ast_diff 与 SymbolVisitor 的缺失导入（当前 NameError 被
    except 吞掉，门禁从未运行）；外层 except Exception: pass 改为向 ast_findings 追加 has_issues=true
    的 blocking finding，使审计内部错误升级为 ESCALATED。'
  target_files:
  - src/adk_dev_flow/workflow.py
  target_symbols: []
- id: T4
  title: 同步文档实测结论
  description: 更新 README.md 的 CPG 条目与 docs/cpg-cas-architecture.md 第 5、6 节：污点门禁的新覆盖范围、作用域隔离、fail-closed
    语义与门禁层行为，只写实测结论。
  target_files:
  - README.md
  - docs/cpg-cas-architecture.md
  target_symbols: []
---

# fix(cpg): 污点门禁 fail-closed 并修复同名参数误报

## Summary
`audit_ast_diff` 的 DFG 污点门禁近乎空转：`param_pass` 边只对通用语句生成，赋值与 return 里的污点汇点（如 `subprocess.getoutput(cmd)`）静态扫描不认、污点分析也够不到；污点源与邻接表按文件全局收集，跨函数同名形参互相串扰（实测 `reader(path)` 的 `path` 被 `processor(data)` 的 `os.system` 误指认）；`except Exception: pass` 吞掉全部内部错误。更严重的是 `workflow.publish_mr_node` 引用了未导入的 `audit_ast_diff`/`SymbolVisitor`，NameError 同样被吞——AST 语义门禁在生产路径上从未真正运行。

修复原则（用户已确认 fail-closed）：审计器内部错误一律抛出，门禁层把审计错误升级为 ESCALATED，宁可拦截不可放行。

## Tasks Breakdown

### Task T1: CpgBuilder 为展平语句补齐 param_pass 边
把 _visit_stmt 里通用分支的调用实参追踪提取为 _trace_call_args，并在 Assign/AnnAssign/AugAssign 取值表达式、Return 取值、If 条件、For/While 迭代与条件上调用，使被展平语句里的实参污点可达。
- **Target Files**: src/adk_dev_flow/cpg.py

### Task T2: audit_ast_diff 污点按函数作用域隔离并 fail-closed
污点 BFS 改为按顶层函数逐个构建（每个函数自己的 param_sources 与 DFG 子图），消除跨函数同名串扰误报，同时保留真阳性（processor 的 data 仍被指认）。DFG 段的 except Exception: pass 改为抛出 AstAuditError（带文件与阶段信息）。
- **Target Files**: src/adk_dev_flow/ast_graph.py

### Task T3: publish_mr_node 审计段补 import 并 fail-closed
workflow.py 补上 audit_ast_diff 与 SymbolVisitor 的缺失导入（当前 NameError 被 except 吞掉，门禁从未运行）；外层 except Exception: pass 改为向 ast_findings 追加 has_issues=true 的 blocking finding，使审计内部错误升级为 ESCALATED。
- **Target Files**: src/adk_dev_flow/workflow.py

### Task T4: 同步文档实测结论
更新 README.md 的 CPG 条目与 docs/cpg-cas-architecture.md 第 5、6 节：污点门禁的新覆盖范围、作用域隔离、fail-closed 语义与门禁层行为，只写实测结论。
- **Target Files**: README.md, docs/cpg-cas-architecture.md

## Locked Acceptance Test (`tests/test_audit_taint_gate.py`)

```python
"""Acceptance tests for GitHub issue #3 (run fb2afbcea33c).

污点门禁 fail-closed 并修复同名参数误报。四个根因各对应一条测试，
修复前每条必须为红（TDD 红灯）：

1. 汇点在赋值语句里（subprocess.getoutput）——静态扫描不认，只有污点边能抓到。
2. 汇点在 return 语句里（subprocess.check_output）——同上。
3. 跨函数同名形参串扰误报——reader 的 path 经文件级 data 桥接被误指认。
4. 审计器内部错误被静默吞掉（fail-open）——必须抛出，且工作流门禁必须拦截。
"""
from __future__ import annotations

import subprocess  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.ast_graph import audit_ast_diff
from adk_dev_flow.cpg import CpgBuilder
from adk_dev_flow.forge_client import BaseGitForge
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import build_dev_workflow


class _DummyForge(BaseGitForge):
    @property
    def platform_name(self) -> str:
        return "dummy"

    @property
    def enabled(self) -> bool:
        return False

    def get_issue(self, issue_iid: int) -> dict:
        return {"iid": issue_iid, "title": "T", "description": "D"}

    def create_or_update_mr(self, **kwargs) -> dict:
        return {"iid": 1, "web_url": "https://example.com/mr/1"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        pass

    def mark_mr_ready(self, mr_iid: int) -> dict:
        return {"iid": mr_iid, "ready": True}


class TestTaintSinkCoverage(unittest.TestCase):
    """根因 1 + 3：汇点只在表达式语句形态可达，赋值/return 里的污点汇点全部漏报。"""

    def test_sink_in_assignment_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "def run(cmd):\n"
            "    out = subprocess.getoutput(cmd)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`cmd`" in s for s in report.dangerous_nodes),
            f"赋值语句里的污点汇点被漏报: {report.dangerous_nodes}",
        )

    def test_sink_in_return_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "def run(code):\n"
            "    return subprocess.check_output(code, shell=True)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`code`" in s for s in report.dangerous_nodes),
            f"return 语句里的污点汇点被漏报: {report.dangerous_nodes}",
        )


class TestTaintScopeIsolation(unittest.TestCase):
    """根因 2：污点源与邻接表按文件全局收集，不同函数的同名形参互相串扰。"""

    def test_same_name_params_do_not_cross_talk(self) -> None:
        code = (
            "import os\n"
            "def reader(path):\n"
            "    data = open(path).read()\n"
            "    return data\n"
            "def processor(data):\n"
            "    os.system(data)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertFalse(
            any("`path`" in s for s in report.dangerous_nodes),
            f"reader 的 path 参数被跨函数串扰误报为污点: {report.dangerous_nodes}",
        )
        # 反向守卫：processor 的 data 是真阳性，必须仍然被指认，防止整体关掉污点分析
        self.assertTrue(
            any("`data`" in s for s in report.dangerous_nodes),
            f"processor 的 data 污点必须仍被检出: {report.dangerous_nodes}",
        )


class TestTaintGateFailsClosed(unittest.TestCase):
    """根因 4：except Exception: pass 吞掉内部错误。"""

    def test_internal_error_raises(self) -> None:
        with patch.object(CpgBuilder, "build", side_effect=RuntimeError("boom")):
            with self.assertRaises(Exception):
                audit_ast_diff("", "def f():\n    pass\n", "t.py")


class TestGateBlocksWhenAuditErrors(unittest.IsolatedAsyncioTestCase):
    """门禁层 fail-closed：审计器抛错时 publish 节点必须 ESCALATED，而不是吞掉后放行。

    前置事实：publish_mr_node 的审计段引用了未导入的 audit_ast_diff /
    SymbolVisitor，NameError 被外层 except Exception: pass 吞掉，门禁从未真正运行。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        git = ["git", "-C", str(self.repo)]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(
            git + ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-q", "-m", "init"],
            check=True,
        )
        base = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        (self.repo / "src" / "svc.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(
            git + ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "change"],
            check=True,
        )
        self.base_sha = base
        self.store = StateStore(self.repo / "state.db")
        self.forge = _DummyForge()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_audit_error_escalates_instead_of_passing(self) -> None:
        run_id = "taint-gate-001"
        self.store.create_run(run_id, issue_iid=3)
        spec = SpecContract(
            issue_iid=3,
            title="Taint gate fail-closed",
            summary="gate audit error must block",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_t.py",
            acceptance_test_code="def test_t():\n    assert True\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task",
                    description="Desc",
                    target_files=["src/svc.py"],
                )
            ],
        )
        self.store.update_run(
            run_id,
            status="VERIFYING",
            spec_json=spec.model_dump_json(),
            worktree_path=str(self.repo),
            branch="feat/taint-gate",
            base_sha=self.base_sha,
        )
        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        publish_node = None
        for edge in wf.edges:
            for item in edge:
                if callable(item) and item.__name__ in ("publish_mr_node", "deliver_mr_node"):
                    publish_node = item
                elif isinstance(item, dict):
                    for sub in item.values():
                        if callable(sub) and sub.__name__ in ("publish_mr_node", "deliver_mr_node"):
                            publish_node = sub
        self.assertIsNotNone(publish_node)

        with patch(
            "adk_dev_flow.workflow.audit_ast_diff",
            side_effect=RuntimeError("audit exploded"),
        ):
            res = await publish_node({"worktree": str(self.repo), "attempt": 0})

        self.assertEqual(
            res.get("status"),
            "ESCALATED",
            f"审计器内部错误必须拦截发布，实际放行: {res}",
        )
        run_record = self.store.get_run(run_id)
        self.assertEqual(run_record["status"], "ESCALATED")


if __name__ == "__main__":
    unittest.main()
```
