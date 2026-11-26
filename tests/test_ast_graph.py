from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.ast_graph import (
    CodeGraph,
    audit_ast_diff,
    generate_skeleton,
)
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore


class TestAstGraphSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmpdir.name)

        # Build a miniature repository
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)

        (self.repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")

        # Module A (core)
        (self.repo / "src" / "pkg" / "core.py").write_text(
            """\"\"\"Core module.\"\"\"
class Service:
    def __init__(self, host: str, port: int = 8080) -> None:
        self.host = host
        self.port = port

    def process(self, data: str) -> bool:
        \"\"\"Process data string.\"\"\"
        return len(data) > 0

def standalone_helper(value: int) -> int:
    return value * 2
""",
            encoding="utf-8",
        )

        # Module B (consumer)
        (self.repo / "src" / "pkg" / "consumer.py").write_text(
            """from pkg.core import Service, standalone_helper

def handle_request(raw: str) -> bool:
    svc = Service("localhost")
    v = standalone_helper(5)
    return svc.process(raw)
""",
            encoding="utf-8",
        )

        # Module C (unrelated)
        (self.repo / "src" / "pkg" / "util.py").write_text(
            """def misc():
    pass
""",
            encoding="utf-8",
        )

        # Test file
        (self.repo / "tests" / "test_core.py").write_text(
            """import unittest
from pkg.core import Service

class TestCore(unittest.TestCase):
    def test_process(self):
        s = Service("127.0.0.1")
        self.assertTrue(s.process("hello"))
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_code_graph_indexing_and_deps(self) -> None:
        cg = CodeGraph(self.repo)
        cg.index_repo()

        self.assertIn("src/pkg/core.py", cg.file_index)
        self.assertIn("src/pkg/consumer.py", cg.file_index)
        self.assertIn("tests/test_core.py", cg.file_index)

        # Core should have dependents: consumer.py and test_core.py
        core_deps = cg.reverse_file_deps.get("src/pkg/core.py", set())
        self.assertIn("src/pkg/consumer.py", core_deps)
        self.assertIn("tests/test_core.py", core_deps)

    def test_analyze_impact_blast_radius(self) -> None:
        cg = CodeGraph(self.repo)
        report = cg.analyze_impact(["src/pkg/core.py"])

        self.assertIn("src/pkg/core.py", report.impacted_production_files)
        self.assertIn("src/pkg/consumer.py", report.impacted_production_files)
        self.assertNotIn("src/pkg/util.py", report.impacted_production_files)
        self.assertIn("tests/test_core.py", report.impacted_test_files)

        # Suggested allowed paths must contain core, consumer, and affected test suites (N7 fix)
        self.assertIn("src/pkg/core.py", report.suggested_allowed_paths)
        self.assertIn("src/pkg/consumer.py", report.suggested_allowed_paths)
        self.assertIn("tests/test_core.py", report.suggested_allowed_paths)

    def test_generate_skeleton_strips_body_and_preserves_signatures(self) -> None:
        code = """import os
from typing import Optional

class Worker:
    \"\"\"Worker documentation.\"\"\"
    active: bool = True

    @classmethod
    def create(cls, name: str) -> "Worker":
        instance = cls()
        instance.name = name
        return instance

    async def run_job(self, timeout: int = 30) -> Optional[str]:
        \"\"\"Run async job.\"\"\"
        data = await fetch(timeout)
        return data
"""
        skeleton = generate_skeleton(code, file_path="worker.py")

        self.assertIn("class Worker:", skeleton)
        self.assertIn('"""Worker documentation."""', skeleton)
        self.assertIn("active: bool = True", skeleton)
        self.assertIn("def create(cls, name: str) -> 'Worker':", skeleton)
        self.assertIn("async def run_job(self, timeout: int=30) -> Optional[str]:", skeleton)
        self.assertIn('"""Run async job."""', skeleton)
        self.assertIn("...", skeleton)
        # Internal body statements should be stripped
        self.assertNotIn("instance = cls()", skeleton)
        self.assertNotIn("data = await fetch(timeout)", skeleton)

    def test_audit_ast_diff_detects_syntax_error(self) -> None:
        base = "def ok(): pass"
        broken = "def broken(:\n    pass"
        diff = audit_ast_diff(base, broken, file_path="app.py")
        self.assertFalse(diff.syntax_ok)
        self.assertIn("SyntaxError", diff.syntax_error or "")
        self.assertTrue(diff.has_issues)

    def test_audit_ast_diff_detects_breaking_changes(self) -> None:
        base = """
def public_api(param1: int, param2: str = 'def') -> bool:
    return True

class PublicClient:
    def execute(self, command: str) -> None:
        pass
"""
        # Case 1: Removed public function
        new_code_1 = """
class PublicClient:
    def execute(self, command: str) -> None:
        pass
"""
        diff1 = audit_ast_diff(base, new_code_1)
        self.assertTrue(any("删除了公共符号 `public_api`" in iss for iss in diff1.breaking_issues))

        # Case 2: Removed required parameter param1
        new_code_2 = """
def public_api(param2: str = 'def') -> bool:
    return True

class PublicClient:
    def execute(self, command: str) -> None:
        pass
"""
        diff2 = audit_ast_diff(base, new_code_2)
        self.assertTrue(any("移除了参数 `param1`" in iss for iss in diff2.breaking_issues))

        # Case 3: Added a required parameter without default
        new_code_3 = """
def public_api(param1: int, new_required: float, param2: str = 'def') -> bool:
    return True

class PublicClient:
    def execute(self, command: str) -> None:
        pass
"""
        diff3 = audit_ast_diff(base, new_code_3)
        self.assertTrue(
            any("新增了无默认值的必填参数 `new_required`" in iss for iss in diff3.breaking_issues)
        )

    def test_audit_ast_diff_detects_dangerous_nodes(self) -> None:
        code = """
import os
import pty

def exploit():
    os.system("rm -rf /")
    eval("1+1")
    API_TOKEN_SECRET = "sk-live-0011223344556677"
"""
        diff = audit_ast_diff("", code)
        self.assertTrue(any("os.system" in d for d in diff.dangerous_nodes))
        self.assertTrue(any("eval" in d for d in diff.dangerous_nodes))
        self.assertTrue(any("pty" in d for d in diff.dangerous_nodes))
        self.assertTrue(any("疑似硬编码凭据" in d for d in diff.dangerous_nodes))

    def test_audit_ast_diff_detects_obfuscated_and_reflection_attacks(self) -> None:
        code = """
import os
import base64

def advanced_exploit():
    # Reflection attack
    fn = getattr(os, "sys" + "tem")
    fn("id")
    # Namespace elevation
    g = globals().get('__builtins__')
    # Obfuscated secret decode
    SECRET_TOKEN = base64.b64decode("c2stMTIzNDU2Nzg5MDEyMzQ1Ng==").decode()
"""
        diff = audit_ast_diff("", code)
        self.assertTrue(any("getattr" in d for d in diff.dangerous_nodes))
        self.assertTrue(any("globals" in d or "__builtins__" in d for d in diff.dangerous_nodes))
        self.assertTrue(any("混淆还原敏感凭据" in d for d in diff.dangerous_nodes))

    def test_audit_ast_diff_supports_noqa_and_allowed_dangerous_calls(self) -> None:
        """P1-1: Verify # noqa: adk-ast suppresses dangerous nodes or breaking changes."""
        code = """
import os
import subprocess

def legitimate_tool():
    subprocess.run(["git", "status"])  # noqa: adk-ast
    os.system("echo ok")  # noqa: adk-ast
    eval("1 + 1")  # unsuppressed
"""
        diff = audit_ast_diff("", code)
        self.assertTrue(any("eval" in d for d in diff.dangerous_nodes))
        self.assertFalse(any("subprocess.run" in d for d in diff.dangerous_nodes))
        self.assertFalse(any("os.system" in d for d in diff.dangerous_nodes))

        # Test allowed_dangerous_calls parameter
        code2 = """
import subprocess
def run_git():
    subprocess.run(["git", "status"])
"""
        diff2 = audit_ast_diff("", code2, allowed_dangerous_calls={"subprocess.run"})
        self.assertFalse(any("subprocess.run" in d for d in diff2.dangerous_nodes))

    def test_generate_skeleton_preserves_nested_functions(self) -> None:
        code = """
def factory(base_val: int):
    \"\"\"Factory function.\"\"\"
    x = 100
    def tool_handler(name: str) -> bool:
        \"\"\"Nested handler.\"\"\"
        return len(name) > 0
    return tool_handler
"""
        skel = generate_skeleton(code)
        self.assertIn("def factory(base_val: int):", skel)
        self.assertIn("def tool_handler(name: str) -> bool:", skel)
        self.assertIn('"""Nested handler."""', skel)
        self.assertNotIn("x = 100", skel)

    def test_flow_service_analyze_impact_and_lock_spec_advisory(self) -> None:
        db_path = self.repo / "test.db"
        store = StateStore(db_path)
        service = FlowService(repo_root=self.repo, store=store)

        # 1. Direct tool call flow_analyze_impact
        res = service.flow_analyze_impact(["src/pkg/core.py"])
        self.assertIn("src/pkg/core.py", res["target_paths"])
        self.assertIn("src/pkg/consumer.py", res["impacted_production_files"])
        self.assertIn("tests/test_core.py", res["impacted_test_files"])

        # 2. flow_lock_spec includes advisory ast_impact
        store.create_run("run-ast-1")
        spec = SpecContract(
            title="Update core",
            summary="Refactor core service",
            allowed_paths=["src/pkg/core.py"],
            acceptance_test_path="tests/test_core.py",
            acceptance_test_code="def test_core(): assert True",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task 1",
                    description="Desc",
                    target_files=["src/pkg/core.py"],
                )
            ],
            require_mutation_gate=False,
        )
        lock_res = service.flow_lock_spec("run-ast-1", spec)
        self.assertEqual(lock_res["status"], "SPEC_LOCKED")
        self.assertIn("ast_impact", lock_res)
        self.assertIn("src/pkg/consumer.py", lock_res["ast_impact"]["impacted_production_files"])


class TestQualifiedSymbolGraph(unittest.TestCase):
    """Issue #5: 模块限定符号表、裸名歧义全匹配与调用点归属规则。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmpdir.name)
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")

        (self.repo / "src" / "pkg" / "mod_a.py").write_text(
            "def dup_fn(x: int) -> int:\n"
            "    return x + 1\n\n"
            "def caller_a() -> int:\n"
            "    return dup_fn(1)\n\n"
            "def unique_a(v: str) -> str:\n"
            "    return v.upper()\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "pkg" / "mod_b.py").write_text(
            "def dup_fn(x: int) -> int:\n"
            "    return x + 2\n\n"
            "def caller_b() -> int:\n"
            "    return dup_fn(2)\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "pkg" / "outside.py").write_text(
            "from pkg.mod_a import unique_a\n\n"
            "def caller_unique() -> str:\n"
            "    return unique_a('hi')\n\n"
            "def caller_ambig() -> int:\n"
            "    return dup_fn(3)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_file_path_query_matches_exact_file_and_scoped_callers(self) -> None:
        with CodeGraph(self.repo) as cg:
            report = cg.analyze_impact(["src/pkg/mod_a.py"])
        self.assertIn("src/pkg/mod_a.py", report.impacted_production_files)
        self.assertNotIn("src/pkg/mod_b.py", report.impacted_production_files)
        self.assertEqual(report.ambiguous_symbols, {})
        # Same-file caller_a and unique_a caller_unique belong to mod_a, NOT caller_b from mod_b
        callers_joined = " ".join(report.impacted_callers)
        self.assertIn("caller_a", callers_joined)
        self.assertIn("caller_unique", callers_joined)
        self.assertNotIn("caller_b", callers_joined)
        self.assertNotIn("caller_ambig", callers_joined)

    def test_unique_bare_symbol_query_unchanged_and_not_ambiguous(self) -> None:
        with CodeGraph(self.repo) as cg:
            report = cg.analyze_impact(["unique_a"])
        self.assertEqual(report.impacted_production_files, ["src/pkg/mod_a.py", "src/pkg/outside.py"])
        self.assertEqual(report.ambiguous_symbols, {})
        self.assertTrue(any("caller_unique" in c for c in report.impacted_callers))

    def test_ambiguous_bare_symbol_matches_all_defining_files(self) -> None:
        with CodeGraph(self.repo) as cg:
            report = cg.analyze_impact(["dup_fn"])
        self.assertIn("src/pkg/mod_a.py", report.impacted_production_files)
        self.assertIn("src/pkg/mod_b.py", report.impacted_production_files)
        self.assertIn("src/pkg/mod_a.py", report.suggested_allowed_paths)
        self.assertIn("src/pkg/mod_b.py", report.suggested_allowed_paths)
        self.assertIn("dup_fn", report.ambiguous_symbols)
        self.assertEqual(
            sorted(report.ambiguous_symbols["dup_fn"]),
            ["src/pkg/mod_a.py", "src/pkg/mod_b.py"],
        )

    def test_qualified_symbol_query_narrows_to_single_module(self) -> None:
        with CodeGraph(self.repo) as cg:
            for qual in ("pkg.mod_a:dup_fn", "src/pkg/mod_a.py:dup_fn", "pkg.mod_a.dup_fn"):
                report = cg.analyze_impact([qual])
                self.assertIn("src/pkg/mod_a.py", report.impacted_production_files, f"query={qual}")
                self.assertNotIn("src/pkg/mod_b.py", report.impacted_production_files, f"query={qual}")
                self.assertEqual(report.ambiguous_symbols, {}, f"query={qual}")
                callers_joined = " ".join(report.impacted_callers)
                self.assertIn("caller_a", callers_joined)
                self.assertNotIn("caller_b", callers_joined)
                self.assertNotIn("caller_ambig", callers_joined)

    def test_unresolved_ambiguous_calls_recorded_not_misattributed(self) -> None:
        with CodeGraph(self.repo) as cg:
            cg.index_repo()
            unresolved_for_outside = cg.unresolved_calls.get("pkg.outside:caller_ambig", set())
        self.assertIn("dup_fn", unresolved_for_outside)

    def test_full_module_aliases_backslash_and_context_generation(self) -> None:
        (self.repo / "tests" / "test_mod_a.py").write_text(
            "from pkg.mod_a import unique_a\n\n"
            "def test_unique_a_case() -> None:\n"
            "    assert unique_a('abc') == 'ABC'\n",
            encoding="utf-8",
        )
        (self.repo / "sub_pkg_tests").mkdir()
        (self.repo / "sub_pkg_tests" / "helper_test.py").write_text(
            "import pkg.mod_a.sub\n\n"
            "def test_sub_import() -> None:\n"
            "    pass\n",
            encoding="utf-8",
        )
        from adk_dev_flow.cpg import CpgStore

        with CpgStore(self.repo / ".adk" / "cpg.db") as store, CodeGraph(self.repo, store=store) as cg:
            self.assertEqual(cg._path_to_module(r"src\pkg\mod_a.py"), ("pkg.mod_a", "src.pkg.mod_a"))
            win_report = cg.analyze_impact([r"src\pkg\mod_a.py"])
            self.assertIn("src/pkg/mod_a.py", win_report.impacted_production_files)
            self.assertIn("tests/test_mod_a.py", win_report.impacted_test_files)
            self.assertIn("sub_pkg_tests/helper_test.py", win_report.impacted_test_files)
            self.assertEqual(
                win_report.summary,
                "目标路径包含 1 个核心文件与 0 个符号；AST 分析共检测到 2 个关联生产文件与 2 个受影响测试套件，识别出 3 个直接调用点。",
            )

            for full_alias in ("src.pkg.mod_a:dup_fn", "src.pkg.mod_a.dup_fn"):
                full_rep = cg.analyze_impact([full_alias])
                self.assertEqual(full_rep.impacted_production_files, ["src/pkg/mod_a.py", "src/pkg/outside.py"])

            ctx_qual = cg.get_test_generation_context(["pkg.mod_a:dup_fn"])
            self.assertEqual(set(ctx_qual.keys()), {"impact", "target_skeletons", "impacted_caller_skeletons", "impacted_tests_info"})
            self.assertIn("src/pkg/mod_a.py", ctx_qual["target_skeletons"])
            self.assertIn("pkg.mod_a:caller_a", ctx_qual["impacted_caller_skeletons"])
            self.assertTrue(
                ctx_qual["impacted_caller_skeletons"]["pkg.mod_a:caller_a"].startswith("src/pkg/mod_a.py:4 ")
            )

            ctx_bare = cg.get_test_generation_context(["dup_fn"])
            self.assertEqual(sorted(ctx_bare["target_skeletons"].keys()), ["src/pkg/mod_a.py", "src/pkg/mod_b.py"])

            ctx_win = cg.get_test_generation_context([r"src\pkg\mod_a.py"])
            self.assertIn("src/pkg/mod_a.py", ctx_win["target_skeletons"])

            ctx_unique = cg.get_test_generation_context(["unique_a"])
            self.assertEqual(
                ctx_unique["impacted_tests_info"],
                {
                    "sub_pkg_tests/helper_test.py": ["test_sub_import"],
                    "tests/test_mod_a.py": ["test_unique_a_case"],
                },
            )

    def test_short_module_collision_falls_back_to_full_module(self) -> None:
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "mod_a.py").write_text(
            "def dup_fn(x: int) -> int:\n    return x + 10\n",
            encoding="utf-8",
        )
        with CodeGraph(self.repo) as cg:
            cg.index_repo()
            self.assertEqual(cg.qualified_symbol_to_file["pkg.mod_a:dup_fn"], "pkg/mod_a.py")
            self.assertEqual(cg.qualified_symbol_to_file["src.pkg.mod_a:dup_fn"], "src/pkg/mod_a.py")
            rep_src = cg.analyze_impact(["src.pkg.mod_a:dup_fn"])
            self.assertEqual(rep_src.impacted_production_files, ["src/pkg/mod_a.py", "src/pkg/outside.py"])
            rep_root = cg.analyze_impact(["pkg.mod_a:dup_fn"])
            self.assertEqual(rep_root.impacted_production_files, ["pkg/mod_a.py"])
            self.assertNotIn("pkg.mod_a.dup_fn", cg._qualified_aliases)

    def test_classification_class_methods_and_two_part_prefix_imports(self) -> None:
        (self.repo / "src" / "solo.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "src" / "pkg" / "tests").mkdir(parents=True)
        (self.repo / "tests" / "helper_only.py").write_text(
            "from pkg.mod_a import unique_a\n\ndef check() -> str:\n    return unique_a('a')\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "pkg" / "tests" / "spec_mod.py").write_text(
            "from pkg.mod_a import unique_a\n\ndef run_spec() -> str:\n    return unique_a('b')\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "pkg" / "test_util.py").write_text(
            "from pkg.mod_a import unique_a\n\ndef helper() -> str:\n    return unique_a('c')\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_cls.py").write_text(
            "from pkg.mod_a import unique_a\n\nclass TestCls:\n    def test_in_cls(self) -> None:\n        assert unique_a('d') == 'D'\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "use_solo.py").write_text(
            "import solo.missing\n",
            encoding="utf-8",
        )
        with CodeGraph(self.repo) as cg:
            rep = cg.analyze_impact(["src/pkg/mod_a.py"])
            self.assertIn("tests/helper_only.py", rep.impacted_test_files)
            self.assertIn("src/pkg/tests/spec_mod.py", rep.impacted_test_files)
            self.assertIn("src/pkg/test_util.py", rep.impacted_test_files)
            self.assertNotIn("tests/helper_only.py", rep.impacted_production_files)
            self.assertNotIn("src/pkg/tests/spec_mod.py", rep.impacted_production_files)
            self.assertNotIn("src/pkg/test_util.py", rep.impacted_production_files)

            ctx = cg.get_test_generation_context(["src/pkg/mod_a.py"])
            self.assertEqual(ctx["impacted_tests_info"]["tests/test_cls.py"], ["TestCls.test_in_cls"])

            solo_rep = cg.analyze_impact(["src/solo.py"])
            self.assertIn("src/use_solo.py", solo_rep.impacted_production_files)


if __name__ == "__main__":
    unittest.main()
