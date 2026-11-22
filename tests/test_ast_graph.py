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

        # Suggested allowed paths should contain core and consumer, but not test
        self.assertIn("src/pkg/core.py", report.suggested_allowed_paths)
        self.assertIn("src/pkg/consumer.py", report.suggested_allowed_paths)
        self.assertNotIn("tests/test_core.py", report.suggested_allowed_paths)

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
        )
        lock_res = service.flow_lock_spec("run-ast-1", spec)
        self.assertEqual(lock_res["status"], "SPEC_LOCKED")
        self.assertIn("ast_impact", lock_res)
        self.assertIn("src/pkg/consumer.py", lock_res["ast_impact"]["impacted_production_files"])


if __name__ == "__main__":
    unittest.main()
