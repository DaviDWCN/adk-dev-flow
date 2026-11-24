from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.mutation import (
    AstMutationEngine,
    BinaryOpMutator,
    ConstantReplacementMutator,
    InvertComparisonMutator,
    Mutant,
    MutationReport,
    ReturnReplacementMutator,
    StatementDeletionMutator,
    evaluate_test_against_mutants,
)


class TestMutationEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sample_code = """
def calculate_discount(price: float, is_vip: bool, coupon: float) -> float:
    if price < 0.0:
        raise ValueError("Negative price")
    discount = 0.0
    if is_vip:
        discount = price * 0.2
    if coupon > 0.0:
        discount = discount + coupon
    final_price = price - discount
    return max(0.0, final_price)
"""

    def test_mutation_operators_generate_diverse_mutants(self) -> None:
        """Requirement 1: AstMutationEngine must synthesize syntactic mutants across AST categories."""
        engine = AstMutationEngine()
        mutants = engine.generate_mutants(self.sample_code)
        self.assertGreater(len(mutants), 5, "Must generate at least 5 mutants for sample code")

        op_names = {m.operator_name for m in mutants}
        self.assertIn("InvertComparison", op_names)
        self.assertIn("BinaryOp", op_names)
        self.assertIn("ReturnReplacement", op_names)

        # Ensure all mutants are syntactically valid Python code
        for m in mutants:
            try:
                tree = ast.parse(m.mutated_source)
                self.assertIsNotNone(tree)
            except SyntaxError as e:
                self.fail(f"Mutant {m.mutant_id} produced invalid syntax: {e}\n{m.mutated_source}")

    async def test_strict_test_achieves_high_mutation_score(self) -> None:
        """Requirement 2: Comprehensive assertions must kill >= 85% of mutants."""
        comprehensive_test = """
import pytest
from target import calculate_discount

def test_negative_price():
    with pytest.raises(ValueError):
        calculate_discount(-10.0, False, 0.0)

def test_boundary_zero_price():
    assert calculate_discount(0.0, False, 0.0) == 0.0

def test_standard_price():
    assert calculate_discount(100.0, False, 0.0) == 100.0

def test_vip_discount():
    assert calculate_discount(100.0, True, 0.0) == 80.0

def test_coupon_discount():
    assert calculate_discount(100.0, False, 15.0) == 85.0

def test_small_coupon():
    assert calculate_discount(100.0, False, 0.5) == 99.5

def test_vip_with_coupon():
    assert calculate_discount(100.0, True, 10.0) == 70.0

def test_coupon_exceeds_price():
    assert calculate_discount(50.0, False, 100.0) == 0.0
"""
        report = await evaluate_test_against_mutants(
            target_source=self.sample_code,
            test_source=comprehensive_test,
            target_module_name="target",
            min_kill_ratio=0.85,
        )
        self.assertIsInstance(report, MutationReport)
        self.assertGreaterEqual(
            report.kill_ratio,
            0.85,
            f"Strict test suite should achieve >= 85% kill ratio (actual: {report.kill_ratio:.1%}, survived: {report.survived_details})",
        )
        self.assertTrue(report.is_acceptable)
        self.assertGreater(report.killed_count, 0)

    async def test_weak_test_fails_mutation_gate_with_diagnostics(self) -> None:
        """Requirement 3: Weak / empty assert test must fail mutation gate (< 85% kill ratio)

        and output survived mutant diagnostics.
        """
        weak_test = """
from target import calculate_discount

def test_calculate_weak():
    # Only executes the code without checking discount calculations or negative price errors
    res = calculate_discount(100.0, True, 10.0)
    assert res is not None
"""
        report = await evaluate_test_against_mutants(
            target_source=self.sample_code,
            test_source=weak_test,
            target_module_name="target",
            min_kill_ratio=0.85,
        )
        self.assertIsInstance(report, MutationReport)
        self.assertFalse(report.is_acceptable, "Weak test must not pass mutation gate")
        self.assertLess(report.kill_ratio, 0.85)
        self.assertGreater(report.survived_count, 0)
        self.assertTrue(len(report.survived_details) > 0, "Must provide survived mutant diagnostics")


if __name__ == "__main__":
    unittest.main()
