from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.ast_graph import CodeGraph
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore


class TestFlowOptimizationSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmpdir.name)

        # Set up a git repo for testing
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)

        (self.repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "pkg" / "math_service.py").write_text(
            '''"""Math service module."""
def calculate_tax(amount: float, rate: float = 0.05) -> float:
    """Calculate tax for a given amount."""
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    return amount * rate
''',
            encoding="utf-8",
        )
        (self.repo / "src" / "pkg" / "order_handler.py").write_text(
            '''from pkg.math_service import calculate_tax

def process_order(total: float) -> float:
    tax = calculate_tax(total)
    return total + tax
''',
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_order.py").write_text(
            '''from pkg.order_handler import process_order

def test_process_order():
    assert process_order(100.0) > 0
''',
            encoding="utf-8",
        )

        self.store = StateStore(self.repo)
        self.service = FlowService(repo_root=self.repo, store=self.store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_ast_test_generation_context(self) -> None:
        """Requirement 1: Test Agent needs AST context (blast radius + skeletons + caller details)."""
        cg = CodeGraph(self.repo)
        ctx = cg.get_test_generation_context(["src/pkg/math_service.py"])

        self.assertIn("impact", ctx)
        self.assertIn("target_skeletons", ctx)
        self.assertIn("src/pkg/math_service.py", ctx["target_skeletons"])
        skeleton = ctx["target_skeletons"]["src/pkg/math_service.py"]
        self.assertIn("def calculate_tax", skeleton)
        self.assertIn("amount: float", skeleton)
        self.assertIn("-> float:", skeleton)
        self.assertIn("...", skeleton)

        self.assertIn("impacted_caller_skeletons", ctx)
        self.assertIn("impacted_tests_info", ctx)
        self.assertIn("tests/test_order.py", ctx["impacted_tests_info"])

        # Service tool flow_analyze_impact should support include_skeletons=True
        impact_res = self.service.flow_analyze_impact(
            ["src/pkg/math_service.py"], include_skeletons=True
        )
        self.assertIn("target_skeletons", impact_res)
        self.assertIn("src/pkg/math_service.py", impact_res["target_skeletons"])

    def test_spec_contract_extended_fields_and_okf(self) -> None:
        """Requirement 2: SpecContract supports design_doc, red_evidence, and pre_critic_report."""
        spec = SpecContract(
            title="Add discount support",
            summary="Support discount calculation",
            allowed_paths=["src/pkg/math_service.py"],
            acceptance_test_path="tests/test_discount.py",
            acceptance_test_code="def test_discount():\n    assert False\n",
            design_doc="## Architecture Design\nIntroduce discount parameter to calculate_tax.",
            red_evidence={
                "exit_code": 1,
                "is_pure_semantic_failure": True,
                "assertion_error_summary": "AssertionError: assert False",
            },
            pre_critic_report={
                "approved": True,
                "consistency_score": 0.95,
                "summary": "Design and tests align completely.",
            },
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Implement discount parameter",
                    description="Update calculate_tax",
                    target_files=["src/pkg/math_service.py"],
                )
            ],
        )

        okf_md = spec.to_okf_markdown()
        self.assertIn("## Architecture Design", okf_md)
        self.assertIn("TDD 基线红灯凭证 (Pre-flight Red Evidence)", okf_md)
        self.assertIn("前置 Critic 审查报告 (Pre-Critic Review)", okf_md)

    def test_state_store_and_flow_lock_from_intermediate_states(self) -> None:
        """Requirement 3: Intermediate states (SPEC_DESIGNED, TEST_GENERATED, PRE_CRITIC_REVIEWED) can lock."""
        self.store.create_run(run_id="run_interm_1")
        run = self.store.get_run("run_interm_1")
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "SPEC_DRAFTING")

        self.store.update_run("run_interm_1", status="SPEC_DESIGNED")
        updated = self.store.get_run("run_interm_1")
        self.assertEqual(updated["status"], "SPEC_DESIGNED")

        spec = SpecContract(
            title="Feature X",
            summary="Summary X",
            allowed_paths=["src/pkg/math_service.py"],
            acceptance_test_path="tests/test_x.py",
            acceptance_test_code="def test_x():\n    assert False\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task X",
                    description="Desc X",
                    target_files=["src/pkg/math_service.py"],
                )
            ],
        )
        lock_res = self.service.flow_lock_spec("run_interm_1", spec, strict_prelock=False)
        self.assertNotIn("error", lock_res)
        self.assertEqual(lock_res["status"], "SPEC_LOCKED")

    def test_preflight_baseline_tdd_red_check(self) -> None:
        """Requirement 4: Baseline preflight execution catches pure assertion failure vs mock pass."""
        # A test with an assertion failure (genuine TDD red)
        red_code = "def test_should_fail():\n    assert False\n"
        res_red = self.service.flow_preflight_test(
            test_path="tests/test_new_red.py", test_code=red_code
        )
        self.assertTrue(res_red["ok"])
        self.assertTrue(res_red["is_pure_semantic_failure"])
        self.assertEqual(res_red["exit_code"], 1)

        # A test with no assertions that passes on baseline (mock pass / fake red)
        pass_code = "def test_should_pass():\n    pass\n"
        res_pass = self.service.flow_preflight_test(
            test_path="tests/test_new_pass.py", test_code=pass_code
        )
        self.assertFalse(res_pass["ok"])
        self.assertIn("直接通过", res_pass["error"])

        # A test with syntax error (fake red)
        syntax_bad_code = "def test_syntax(\n"
        res_syntax = self.service.flow_preflight_test(
            test_path="tests/test_new_syntax.py", test_code=syntax_bad_code
        )
        self.assertFalse(res_syntax["ok"])
        self.assertIn("语法", res_syntax["error"])

    def test_preflight_does_not_rewrite_status_after_draft(self) -> None:
        red_code = "def test_should_fail():\n    assert False\n"
        path = "tests/test_status_red.py"

        self.store.create_run("run-red")
        drafted = self.service.flow_preflight_test(
            test_path=path, test_code=red_code, run_id="run-red"
        )
        self.assertTrue(drafted["ok"])
        self.assertTrue(drafted["status_updated"])
        self.assertEqual(self.store.get_run("run-red")["status"], "TEST_GENERATED")

        again = self.service.flow_preflight_test(
            test_path=path, test_code=red_code, run_id="run-red"
        )
        self.assertTrue(again["status_updated"])
        self.assertEqual(self.store.get_run("run-red")["status"], "TEST_GENERATED")

        self.store.update_run("run-red", status="RUNNING")
        busy = self.service.flow_preflight_test(
            test_path=path, test_code=red_code, run_id="run-red"
        )
        self.assertTrue(busy["ok"])
        self.assertFalse(busy["status_updated"])
        self.assertEqual(busy["status"], "RUNNING")
        self.assertEqual(self.store.get_run("run-red")["status"], "RUNNING")

        self.store.update_run("run-red", status="SPEC_LOCKED")
        locked = self.service.flow_preflight_test(
            test_path=path, test_code=red_code, run_id="run-red"
        )
        self.assertTrue(locked["ok"])
        self.assertFalse(locked["status_updated"])
        self.assertEqual(self.store.get_run("run-red")["status"], "SPEC_LOCKED")

        missing = self.service.flow_preflight_test(
            test_path=path, test_code=red_code, run_id="missing-run"
        )
        self.assertTrue(missing["ok"])
        self.assertFalse(missing["status_updated"])
        self.assertIn("未知 run_id", missing["status_error"])

    def test_step8_deterministic_override_on_ast_violation(self) -> None:
        """Requirement 5: Deterministic fail-closed override: Critic AI cannot approve breaking AST changes."""
        from adk_dev_flow.workflow import enforce_deterministic_verdict_guard

        # Suppose Critic LLM said approved=True, but AST diff found dangerous eval call or breaking signature
        ast_findings = [
            {
                "file_path": "src/pkg/math_service.py",
                "breaking_issues": ["符号 `calculate_tax` 移除了参数 `rate` (破坏向后兼容性)"],
                "dangerous_nodes": ["发现危险函数调用 `eval` (line 10)"],
                "has_issues": True,
            }
        ]
        raw_critic_verdict = {
            "approved": True,  # Non-deterministic LLM hallucinated approval!
            "confidence": 0.9,
            "summary": "Looks great to me!",
            "blocking_issues": [],
        }

        guarded = enforce_deterministic_verdict_guard(raw_critic_verdict, ast_findings)
        self.assertFalse(guarded["approved"])
        self.assertTrue(len(guarded["blocking_issues"]) >= 2)
        self.assertIn("AST 静态契约门禁一票否决", guarded["summary"])

    def test_ast_preflight_context_building(self) -> None:
        """P0-2: Verify AST skeletons and caller context are extracted for target files."""
        from adk_dev_flow.workflow import _build_ast_context

        ast_ctx = _build_ast_context(self.repo, ["src/pkg/math_service.py"])
        self.assertIn("代码结构与现有签名上下文", ast_ctx)
        self.assertIn("calculate_tax", ast_ctx)
        self.assertIn("math_service.py", ast_ctx)
        self.assertIn("order_handler.py", ast_ctx)


if __name__ == "__main__":
    unittest.main()
