from __future__ import annotations

import ast
import asyncio
import copy
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adk_dev_flow.subproc import no_window_kwargs, run_subprocess_safe
from adk_dev_flow.worktree_utils import get_preferred_python_interpreter


@dataclass
class Mutant:
    mutant_id: str
    operator_name: str
    lineno: int
    original_source: str
    mutated_source: str
    description: str


@dataclass
class MutationReport:
    total_mutants: int
    killed_count: int
    survived_count: int
    kill_ratio: float
    is_acceptable: bool
    survived_details: list[str] = field(default_factory=list)
    killed_details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_mutants": self.total_mutants,
            "killed_count": self.killed_count,
            "survived_count": self.survived_count,
            "kill_ratio": round(self.kill_ratio, 4),
            "is_acceptable": self.is_acceptable,
            "survived_details": self.survived_details,
            "killed_details": self.killed_details,
        }


class InvertComparisonMutator:
    COMPARE_MAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
        ast.Lt: ast.GtE,
        ast.Gt: ast.LtE,
        ast.LtE: ast.Gt,
        ast.GtE: ast.Lt,
        ast.Eq: ast.NotEq,
        ast.NotEq: ast.Eq,
        ast.In: ast.NotIn,
        ast.NotIn: ast.In,
        ast.Is: ast.IsNot,
        ast.IsNot: ast.Is,
    }

    def find_targets(self, tree: ast.AST) -> list[tuple[ast.Compare, int, type[ast.cmpop]]]:
        targets = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for idx, op in enumerate(node.ops):
                    if type(op) in self.COMPARE_MAP:
                        targets.append((node, idx, self.COMPARE_MAP[type(op)]))
        return targets


class BinaryOpMutator:
    BINOP_MAP: dict[type[ast.operator], type[ast.operator]] = {
        ast.Add: ast.Sub,
        ast.Sub: ast.Add,
        ast.Mult: ast.Div,
        ast.Div: ast.Mult,
        ast.FloorDiv: ast.Mod,
        ast.Mod: ast.FloorDiv,
    }

    def find_targets(self, tree: ast.AST) -> list[tuple[ast.BinOp, type[ast.operator]]]:
        targets = []
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and type(node.op) in self.BINOP_MAP:
                targets.append((node, self.BINOP_MAP[type(node.op)]))
        return targets


class ConstantReplacementMutator:
    def find_targets(self, tree: ast.AST) -> list[tuple[ast.Constant, Any]]:
        targets = []
        # Build parent mapping to detect context (docstring, raise, etc.)
        parent_map: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parent_map[child] = parent

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                parent = parent_map.get(node)

                # Skip docstrings
                if isinstance(parent, ast.Expr):
                    grandparent = parent_map.get(parent)
                    if isinstance(grandparent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                        if grandparent.body and grandparent.body[0] is parent:
                            continue

                # Skip string literals inside raise statements (avoid brittle error message matching)
                if isinstance(node.value, str):
                    curr = parent
                    inside_raise = False
                    while curr is not None:
                        if isinstance(curr, ast.Raise):
                            inside_raise = True
                            break
                        curr = parent_map.get(curr)
                    if inside_raise:
                        continue

                val = node.value
                if isinstance(val, bool):
                    targets.append((node, not val))
                elif isinstance(val, (int, float)) and not isinstance(val, bool):
                    new_val = val + 1 if val == 0 else (0 if val > 0 else 1)
                    targets.append((node, new_val))
                elif isinstance(val, str) and 0 < len(val) < 50:
                    targets.append((node, f"mutant_{val}"))
        return targets


class ReturnReplacementMutator:
    def find_targets(self, tree: ast.AST) -> list[ast.Return]:
        targets = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value is not None:
                targets.append(node)
        return targets


class StatementDeletionMutator:
    def find_targets(self, tree: ast.AST) -> list[ast.stmt]:
        targets = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in node.body:
                    if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.Expr)):
                        targets.append(stmt)
        return targets


class AstMutationEngine:
    """Synthesize high-coverage syntactic mutants using AST transformations."""

    def __init__(self) -> None:
        self.cmp_mutator = InvertComparisonMutator()
        self.bin_mutator = BinaryOpMutator()
        self.const_mutator = ConstantReplacementMutator()
        self.ret_mutator = ReturnReplacementMutator()
        self.stmt_mutator = StatementDeletionMutator()

    def generate_mutants(self, source_code: str, max_mutants: int = 50) -> list[Mutant]:
        try:
            base_tree = ast.parse(source_code)
        except SyntaxError:
            return []

        base_unparsed = ast.unparse(base_tree)
        mutants: list[Mutant] = []
        counter = 0

        # 1. Comparison mutations
        for node, op_idx, new_op_cls in self.cmp_mutator.find_targets(base_tree):
            if len(mutants) >= max_mutants:
                break
            tree_copy = copy.deepcopy(base_tree)
            # Find matching node by location
            target_node = self._find_matching_node(tree_copy, node)
            if target_node and isinstance(target_node, ast.Compare) and op_idx < len(target_node.ops):
                orig_op_name = type(target_node.ops[op_idx]).__name__
                target_node.ops[op_idx] = new_op_cls()
                try:
                    mutated_src = ast.unparse(tree_copy)
                    if mutated_src != base_unparsed:
                        counter += 1
                        mutants.append(
                            Mutant(
                                mutant_id=f"MUT-CMP-{counter:03d}",
                                operator_name="InvertComparison",
                                lineno=getattr(node, "lineno", 1),
                                original_source=source_code,
                                mutated_source=mutated_src,
                                description=f"反转比较操作符 `{orig_op_name}` -> `{new_op_cls.__name__}` (line {getattr(node, 'lineno', 1)})",
                            )
                        )
                except Exception:
                    pass

        # 2. Binary operator mutations
        for node, new_op_cls in self.bin_mutator.find_targets(base_tree):
            if len(mutants) >= max_mutants:
                break
            tree_copy = copy.deepcopy(base_tree)
            target_node = self._find_matching_node(tree_copy, node)
            if target_node and isinstance(target_node, ast.BinOp):
                orig_op_name = type(target_node.op).__name__
                target_node.op = new_op_cls()
                try:
                    mutated_src = ast.unparse(tree_copy)
                    if mutated_src != base_unparsed:
                        counter += 1
                        mutants.append(
                            Mutant(
                                mutant_id=f"MUT-BIN-{counter:03d}",
                                operator_name="BinaryOp",
                                lineno=getattr(node, "lineno", 1),
                                original_source=source_code,
                                mutated_source=mutated_src,
                                description=f"变异二元算术运算 `{orig_op_name}` -> `{new_op_cls.__name__}` (line {getattr(node, 'lineno', 1)})",
                            )
                        )
                except Exception:
                    pass

        # 3. Return replacement mutations
        for node in self.ret_mutator.find_targets(base_tree):
            if len(mutants) >= max_mutants:
                break
            tree_copy = copy.deepcopy(base_tree)
            target_node = self._find_matching_node(tree_copy, node)
            if target_node and isinstance(target_node, ast.Return):
                target_node.value = ast.Constant(value=None)
                try:
                    mutated_src = ast.unparse(tree_copy)
                    if mutated_src != base_unparsed:
                        counter += 1
                        mutants.append(
                            Mutant(
                                mutant_id=f"MUT-RET-{counter:03d}",
                                operator_name="ReturnReplacement",
                                lineno=getattr(node, "lineno", 1),
                                original_source=source_code,
                                mutated_source=mutated_src,
                                description=f"篡改函数返回值为 None (line {getattr(node, 'lineno', 1)})",
                            )
                        )
                except Exception:
                    pass

        # 4. Constant replacement mutations
        for node, new_val in self.const_mutator.find_targets(base_tree):
            if len(mutants) >= max_mutants:
                break
            tree_copy = copy.deepcopy(base_tree)
            target_node = self._find_matching_node(tree_copy, node)
            if target_node and isinstance(target_node, ast.Constant):
                target_node.value = new_val
                try:
                    mutated_src = ast.unparse(tree_copy)
                    if mutated_src != base_unparsed:
                        counter += 1
                        mutants.append(
                            Mutant(
                                mutant_id=f"MUT-CONST-{counter:03d}",
                                operator_name="ConstantReplacement",
                                lineno=getattr(node, "lineno", 1),
                                original_source=source_code,
                                mutated_source=mutated_src,
                                description=f"常量变异 `{node.value!r}` -> `{new_val!r}` (line {getattr(node, 'lineno', 1)})",
                            )
                        )
                except Exception:
                    pass

        # 5. Statement deletion mutations
        for stmt in self.stmt_mutator.find_targets(base_tree):
            if len(mutants) >= max_mutants:
                break
            tree_copy = copy.deepcopy(base_tree)
            target_node = self._find_matching_node(tree_copy, stmt)
            if target_node:
                pass_node = ast.Pass()
                ast.copy_location(pass_node, target_node)
                self._replace_stmt(tree_copy, target_node, pass_node)
                try:
                    mutated_src = ast.unparse(tree_copy)
                    if mutated_src != base_unparsed:
                        counter += 1
                        mutants.append(
                            Mutant(
                                mutant_id=f"MUT-STMT-{counter:03d}",
                                operator_name="StatementDeletion",
                                lineno=getattr(stmt, "lineno", 1),
                                original_source=source_code,
                                mutated_source=mutated_src,
                                description=f"剔除关键执行语句 (替换为 pass) (line {getattr(stmt, 'lineno', 1)})",
                            )
                        )
                except Exception:
                    pass

        return mutants

    def _find_matching_node(self, root: ast.AST, target: ast.AST) -> ast.AST | None:
        target_line = getattr(target, "lineno", -1)
        target_col = getattr(target, "col_offset", -1)
        target_type = type(target)

        for node in ast.walk(root):
            if (
                type(node) is target_type
                and getattr(node, "lineno", -1) == target_line
                and getattr(node, "col_offset", -1) == target_col
            ):
                return node
        return None

    def _replace_stmt(self, root: ast.AST, old_stmt: ast.stmt, new_stmt: ast.stmt) -> None:
        for node in ast.walk(root):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(node, field, None)
                if isinstance(stmts, list):
                    for idx, s in enumerate(stmts):
                        if (
                            type(s) is type(old_stmt)
                            and getattr(s, "lineno", -1) == getattr(old_stmt, "lineno", -1)
                            and getattr(s, "col_offset", -1) == getattr(old_stmt, "col_offset", -1)
                        ):
                            stmts[idx] = new_stmt
                            return


def _run_pytest_once(
    py_bin: str,
    test_file: Path,
    cwd: Path,
    timeout_sec: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """Execute a single isolated pytest check inside the mutation sandbox."""
    cmd = [py_bin, "-B", "-m", "pytest", "-q", str(test_file)]
    return subprocess.run(  # noqa: adk-ast
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_sec,
        **no_window_kwargs(),
        env=env,
    )


def evaluate_test_against_mutants_sync(
    target_source: str,
    test_source: str,
    *,
    target_module_name: str = "target",
    min_kill_ratio: float = 0.85,
    max_mutants: int = 50,
    timeout_sec: float = 10.0,
) -> MutationReport:
    """Run acceptance test against synthesized syntactic mutants synchronously."""
    engine = AstMutationEngine()
    mutants = engine.generate_mutants(target_source, max_mutants=max_mutants)
    if not mutants:
        return MutationReport(
            total_mutants=0,
            killed_count=0,
            survived_count=0,
            kill_ratio=1.0,
            is_acceptable=True,
            survived_details=[],
            killed_details=[],
        )

    py_bin = get_preferred_python_interpreter()
    killed_details: list[str] = []
    survived_details: list[str] = []

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        test_file = tmp_dir / "test_spec.py"
        test_file.write_text(test_source, encoding="utf-8")

        if target_module_name.endswith(".py"):
            target_rel = Path(target_module_name)
        else:
            target_rel = Path(f"{target_module_name}.py")

        target_file = tmp_dir / target_rel
        target_file.parent.mkdir(parents=True, exist_ok=True)

        # Create __init__.py up to tmp_dir
        curr = target_file.parent
        while curr != tmp_dir:
            init_f = curr / "__init__.py"
            if not init_f.exists():
                init_f.write_text("", encoding="utf-8")
            curr = curr.parent

        # Also support flat import
        flat_target = tmp_dir / target_file.name

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_py = env.get("PYTHONPATH", "")
        sep = ";" if os.name == "nt" else ":"
        env["PYTHONPATH"] = f"{tmp_dir}{sep}{existing_py}" if existing_py else str(tmp_dir)

        # Baseline sanity check: test must pass on unmutated target_source, otherwise
        # every mutant would fail for the same baseline reason (100% fake kill ratio).
        target_file.write_text(target_source, encoding="utf-8")
        if flat_target != target_file:
            flat_target.write_text(target_source, encoding="utf-8")
        try:
            base_res = _run_pytest_once(py_bin, test_file, tmp_dir, timeout_sec, env)
        except Exception as exc:
            return MutationReport(
                total_mutants=len(mutants),
                killed_count=0,
                survived_count=len(mutants),
                kill_ratio=0.0,
                is_acceptable=False,
                survived_details=[f"[BASELINE_ERROR] 未变异基线测试执行异常: {exc}"],
                killed_details=[],
            )
        if base_res.returncode != 0:
            return MutationReport(
                total_mutants=len(mutants),
                killed_count=0,
                survived_count=len(mutants),
                kill_ratio=0.0,
                is_acceptable=False,
                survived_details=[
                    f"[BASELINE_FAILED] 未变异基线代码下测试未通过 (exit={base_res.returncode})，无法有效度量变异击杀率"
                ],
                killed_details=[],
            )

        for m in mutants:
            target_file.write_text(m.mutated_source, encoding="utf-8")
            if flat_target != target_file:
                flat_target.write_text(m.mutated_source, encoding="utf-8")
            try:
                res = _run_pytest_once(py_bin, test_file, tmp_dir, timeout_sec, env)
                if res.returncode != 0:
                    killed_details.append(f"[{m.mutant_id}] {m.description}")
                else:
                    survived_details.append(f"[{m.mutant_id}] {m.description}")
            except subprocess.TimeoutExpired:
                # Timed out mutant counts as killed (caused infinite loop or hang)
                killed_details.append(f"[{m.mutant_id}] {m.description} (Timeout)")
            except Exception:
                survived_details.append(f"[{m.mutant_id}] {m.description}")

    total = len(mutants)
    killed = len(killed_details)
    survived = len(survived_details)
    kill_ratio = killed / total if total > 0 else 1.0
    is_acceptable = kill_ratio >= min_kill_ratio

    return MutationReport(
        total_mutants=total,
        killed_count=killed,
        survived_count=survived,
        kill_ratio=kill_ratio,
        is_acceptable=is_acceptable,
        survived_details=survived_details,
        killed_details=killed_details,
    )


async def evaluate_test_against_mutants(
    target_source: str,
    test_source: str,
    *,
    target_module_name: str = "target",
    min_kill_ratio: float = 0.85,
    max_mutants: int = 50,
    timeout_sec: float = 10.0,
) -> MutationReport:
    """Run acceptance test against synthesized syntactic mutants to measure fault-killing effectiveness."""
    return await asyncio.to_thread(
        evaluate_test_against_mutants_sync,
        target_source,
        test_source,
        target_module_name=target_module_name,
        min_kill_ratio=min_kill_ratio,
        max_mutants=max_mutants,
        timeout_sec=timeout_sec,
    )
