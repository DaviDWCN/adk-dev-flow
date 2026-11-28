from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore


class TestMutationGateService(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp_dir.name)
        (self.repo_root / ".git").mkdir()

        # Create target production file in repo
        src_dir = self.repo_root / "src"
        src_dir.mkdir(parents=True)
        self.target_py = src_dir / "calculator.py"
        self.target_py.write_text(
            '''def calculate_tax(income: float, is_resident: bool) -> float:
    if income <= 0.0:
        return 0.0
    rate = 0.2 if is_resident else 0.3
    tax = income * rate
    return max(0.0, tax)
''',
            encoding="utf-8",
        )

        self.db_path = self.repo_root / ".adk" / "state.db"
        self.store = StateStore(self.db_path)
        self.service = FlowService(repo_root=self.repo_root, store=self.store)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def test_weak_test_rejected_at_lock_spec(self) -> None:
        """Requirement: Weak assertion test (< 85% kill ratio) MUST be rejected by flow_lock_spec."""
        init_res = await self.service.flow_init(requirement="Implement tax calculator")
        run_id = init_res["run_id"]

        weak_test_code = """
import pytest
from src.calculator import calculate_tax

def test_tax_weak():
    res = calculate_tax(100.0, True)
    assert res is not None
"""
        spec = SpecContract(
            title="Tax Calc",
            summary="Tax Calc Summary",
            allowed_paths=["src/calculator.py"],
            acceptance_test_path="tests/test_tax.py",
            acceptance_test_code=weak_test_code,
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Calc Tax",
                    description="Implement tax",
                    target_files=["src/calculator.py"],
                )
            ],
            require_tdd_red=False,
            require_mutation_gate=True,
        )

        lock_res = self.service.flow_lock_spec(run_id, spec, strict_prelock=False)
        self.assertIn("error", lock_res, "Must reject lock for weak test")
        self.assertIn("变异测试门禁未通过", lock_res["error"])
        self.assertIn("mutation_report", lock_res)
        self.assertFalse(lock_res["mutation_report"]["is_acceptable"])
        self.assertLess(lock_res["mutation_report"]["kill_ratio"], 0.85)

    async def test_strict_test_approved_at_lock_spec(self) -> None:
        """Requirement: Comprehensive test (>= 85% kill ratio) MUST be accepted by flow_lock_spec."""
        init_res = await self.service.flow_init(requirement="Implement tax calculator")
        run_id = init_res["run_id"]

        strict_test_code = """
import pytest
from src.calculator import calculate_tax

def test_zero_or_negative_income():
    assert calculate_tax(0.0, True) == 0.0
    assert calculate_tax(-10.0, True) == 0.0

def test_small_income_boundary():
    assert round(calculate_tax(0.5, True), 2) == 0.10

def test_resident_tax():
    assert calculate_tax(100.0, True) == 20.0
    assert calculate_tax(1000.0, True) == 200.0

def test_non_resident_tax():
    assert calculate_tax(100.0, False) == 30.0
    assert calculate_tax(1000.0, False) == 300.0
"""
        spec = SpecContract(
            title="Tax Calc",
            summary="Tax Calc Summary",
            allowed_paths=["src/calculator.py"],
            acceptance_test_path="tests/test_tax.py",
            acceptance_test_code=strict_test_code,
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Calc Tax",
                    description="Implement tax",
                    target_files=["src/calculator.py"],
                )
            ],
            require_tdd_red=False,
            require_mutation_gate=True,
        )

        lock_res = self.service.flow_lock_spec(run_id, spec, strict_prelock=False)
        self.assertNotIn("error", lock_res, f"Strict test should lock successfully: {lock_res.get('error')}")
        self.assertEqual(lock_res["status"], "SPEC_LOCKED")
        self.assertIn("mutation_report", lock_res)
        self.assertTrue(lock_res["mutation_report"]["is_acceptable"])
        self.assertGreaterEqual(lock_res["mutation_report"]["kill_ratio"], 0.85)
