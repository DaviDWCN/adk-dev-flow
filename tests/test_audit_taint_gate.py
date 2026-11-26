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

    def test_sink_in_class_method_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "class Runner:\n"
            "    def execute(self, cmd):\n"
            "        out = subprocess.getoutput(cmd)\n"
            "        return out\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`cmd`" in s for s in report.dangerous_nodes),
            f"类方法内的污点汇点被漏报: {report.dangerous_nodes}",
        )

    def test_sink_via_keyword_argument_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "def run(cmd):\n"
            "    return subprocess.check_output(args=cmd)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`cmd`" in s for s in report.dangerous_nodes),
            f"关键字参数传递的污点汇点被漏报: {report.dangerous_nodes}",
        )

    def test_kwonly_and_vararg_taint_is_detected(self) -> None:
        code = (
            "import subprocess\n"
            "def run_kw(*, cmd):\n"
            "    return subprocess.getoutput(cmd)\n"
            "def run_var(*cmds):\n"
            "    return subprocess.getoutput(cmds)\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertTrue(
            any("污点" in s and "`cmd`" in s for s in report.dangerous_nodes),
            f"kwonlyargs 形参污点被漏报: {report.dangerous_nodes}",
        )
        self.assertTrue(
            any("污点" in s and "`cmds`" in s for s in report.dangerous_nodes),
            f"vararg 形参污点被漏报: {report.dangerous_nodes}",
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

    def test_class_methods_same_name_params_do_not_cross_talk(self) -> None:
        code = (
            "import subprocess\n"
            "class Pipeline:\n"
            "    def __init__(self, repo_root):\n"
            "        self.repo_root = repo_root\n"
            "    def reader(self, path):\n"
            "        data = open(path).read()\n"
            "        return data\n"
            "    def processor(self, data):\n"
            "        return subprocess.getoutput(data)\n"
            "    def safe_git(self):\n"
            "        return subprocess.check_output(['git', 'status'], cwd=str(self.repo_root))\n"
        )
        report = audit_ast_diff("", code, "t.py")
        self.assertFalse(
            any("`path`" in s for s in report.dangerous_nodes),
            f"reader 的 path 参数被跨方法串扰误报: {report.dangerous_nodes}",
        )
        self.assertFalse(
            any("`self`" in s for s in report.dangerous_nodes),
            f"self 不应被当作不可信外部形参污点源: {report.dangerous_nodes}",
        )
        self.assertTrue(
            any("`data`" in s for s in report.dangerous_nodes),
            f"processor 的 data 污点必须检出: {report.dangerous_nodes}",
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
