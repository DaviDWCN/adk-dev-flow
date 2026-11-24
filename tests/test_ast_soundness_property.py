from __future__ import annotations

import ast
import random
import unittest

from adk_dev_flow.ast_graph import (
    SymbolVisitor,
    audit_ast_diff,
    generate_skeleton,
)


class GenerativeAstFuzzer:
    """Deterministic generative AST synthesizer for property-based soundness testing."""

    PARAM_NAMES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
    FUNC_NAMES = ["compute", "process", "handle", "transform", "evaluate", "dispatch", "parse"]
    CLASS_NAMES = ["Worker", "Service", "Pipeline", "Controller", "Engine", "Adapter"]

    def __init__(self, seed: int = 42) -> None:
        self.rng = random.Random(seed)

    def generate_random_module(self, num_defs: int = 5, allow_nested: bool = True) -> str:
        lines: list[str] = ["# Auto-generated Property AST"]
        for i in range(num_defs):
            if self.rng.random() < 0.3:
                lines.append(self.generate_random_class(allow_nested=allow_nested))
            else:
                lines.append(self.generate_random_func(allow_nested=allow_nested))
        return "\n\n".join(lines)

    def generate_random_class(self, allow_nested: bool = True) -> str:
        cname = f"{self.rng.choice(self.CLASS_NAMES)}_{self.rng.randint(100, 999)}"
        doc = f'    """Class {cname} documentation."""'
        methods: list[str] = []
        for _ in range(self.rng.randint(1, 3)):
            m_code = self.generate_random_func(is_method=True, allow_nested=allow_nested)
            # Indent
            methods.append("\n".join(f"    {line}" for line in m_code.splitlines()))
        return f"class {cname}:\n{doc}\n" + "\n\n".join(methods)

    def generate_random_func(self, is_method: bool = False, allow_nested: bool = True) -> str:
        fname = f"{self.rng.choice(self.FUNC_NAMES)}_{self.rng.randint(100, 999)}"
        params: list[str] = ["self"] if is_method else []

        # Positional / standard args
        n_pos = self.rng.randint(0, 3)
        available_names = list(self.PARAM_NAMES)
        self.rng.shuffle(available_names)

        pos_args = available_names[:n_pos]
        # Assign defaults to last k positional args
        n_defaults = self.rng.randint(0, n_pos) if n_pos > 0 else 0
        default_start_idx = n_pos - n_defaults

        for idx, p in enumerate(pos_args):
            annot = ": int" if self.rng.random() < 0.5 else ""
            if idx >= default_start_idx:
                def_val = str(self.rng.randint(0, 100))
                params.append(f"{p}{annot} = {def_val}")
            else:
                params.append(f"{p}{annot}")

        has_vararg = False
        # Varargs
        if self.rng.random() < 0.3:
            params.append("*args")
            has_vararg = True

        # Kwonly args
        n_kwonly = self.rng.randint(0, 2)
        if n_kwonly > 0:
            if not has_vararg:
                params.append("*")
            for _ in range(n_kwonly):
                kw_name = f"kw_{self.rng.choice(self.PARAM_NAMES)}"
                if self.rng.random() < 0.5:
                    params.append(f"{kw_name}: str = 'default'")
                else:
                    params.append(f"{kw_name}: str")

        # Kwargs
        if self.rng.random() < 0.2:
            params.append("**kwargs")

        sig = f"def {fname}({', '.join(params)}) -> bool:"
        doc = f'    """Function {fname} summary."""'
        body_lines: list[str] = [sig, doc]

        # Nested closure
        if allow_nested and self.rng.random() < 0.3:
            inner_func = self.generate_random_func(is_method=False, allow_nested=False)
            indented_inner = "\n".join(f"    {line}" for line in inner_func.splitlines())
            body_lines.append(indented_inner)
            body_lines.append("    return True")
        else:
            body_lines.append("    res = 42")
            body_lines.append("    return res > 0")

        return "\n".join(body_lines)


class TestAstSoundnessProperty(unittest.TestCase):
    def setUp(self) -> None:
        self.fuzzer = GenerativeAstFuzzer(seed=1337)

    def test_property_1_random_valid_ast_never_crashes_auditor_or_skeletonizer(self) -> None:
        """Property 1 (Robustness): Syntactically valid Python ASTs must never crash audit_ast_diff or generate_skeleton."""
        for i in range(50):
            code = self.fuzzer.generate_random_module(num_defs=4)
            # Verify valid Python syntax
            tree = ast.parse(code)
            self.assertIsNotNone(tree)

            # SUT 1: generate_skeleton must never raise unhandled exception
            skel = generate_skeleton(code, file_path="test_fuzz.py")
            self.assertIsInstance(skel, str)

            # SUT 2: audit_ast_diff must never raise unhandled exception
            report = audit_ast_diff(code, code, file_path="test_fuzz.py")
            self.assertTrue(report.syntax_ok)
            self.assertFalse(report.has_issues)

    def test_property_2_adding_default_value_is_never_a_breaking_change(self) -> None:
        """Property 2 (Soundness): Changing 'def f(x)' to 'def f(x = 10)' is backwards-compatible.

        Must NEVER be reported as a breaking issue!
        """
        base = """
def process_data(payload: dict, timeout: int) -> bool:
    return True
"""
        # Timeout given a default value
        new_code = """
def process_data(payload: dict, timeout: int = 30) -> bool:
    return True
"""
        report = audit_ast_diff(base, new_code)
        self.assertFalse(report.has_issues, f"Unexpected breaking issues: {report.breaking_issues}")

    def test_property_3_internal_closure_refactoring_is_never_a_public_breaking_change(self) -> None:
        """Property 3 (Precision): Renaming or removing a nested helper inside a function body

        is an internal refactoring and must NEVER be reported as deleting a public API symbol!
        """
        base = """
def public_service_endpoint(request_id: str) -> bool:
    def internal_helper(x: int) -> int:
        return x * 2
    return internal_helper(10) > 0
"""
        # Renamed internal helper to local_calc
        new_code = """
def public_service_endpoint(request_id: str) -> bool:
    def local_calc(x: int) -> int:
        return x * 2
    return local_calc(10) > 0
"""
        report = audit_ast_diff(base, new_code)
        # Precision invariant: internal_helper is a local closure, NOT a public module API!
        self.assertFalse(
            any("internal_helper" in iss for iss in report.breaking_issues),
            f"False positive: internal closure reported as public symbol: {report.breaking_issues}",
        )

    def test_property_4_suppression_and_whitelisting_soundness(self) -> None:
        """Property 4 (Configurability): Line suppression (# noqa: adk-ast) and allowed_dangerous_calls
        must accurately suppress dangerous nodes and intentional breaking changes.
        """
        code_dangerous = """
def run_command():
    os.system("echo ok")  # noqa: adk-ast
"""
        report1 = audit_ast_diff("", code_dangerous)
        self.assertFalse(report1.has_issues, f"Suppressed dangerous call should not trigger issues: {report1.dangerous_nodes}")

        code_whitelist = """
def run_command():
    os.system("echo ok")
"""
        report2 = audit_ast_diff("", code_whitelist, allowed_dangerous_calls={"os.system"})
        self.assertFalse(report2.has_issues, f"Whitelisted call should not trigger issues: {report2.dangerous_nodes}")

        # Intentional breaking change with noqa suppression
        base = "def compute(x: int) -> int: return x\n"
        new_suppressed = "def compute(x: int, y: int) -> int:  # noqa: adk-ast\n    return x + y\n"
        report3 = audit_ast_diff(base, new_suppressed)
        self.assertFalse(report3.has_issues, f"Suppressed signature change should not be breaking: {report3.breaking_issues}")

    def test_property_5_removing_default_is_detected_as_breaking_change(self) -> None:
        """Property 5 (Soundness): Changing 'def f(x=1)' to 'def f(x)' breaks callers that omitted x."""
        base = "def connect(timeout: int = 10) -> bool: return True\n"
        new_code = "def connect(timeout: int) -> bool: return True\n"
        report = audit_ast_diff(base, new_code)
        self.assertTrue(report.has_issues)
        self.assertTrue(any("timeout" in iss and "改为无默认值" in iss for iss in report.breaking_issues))


if __name__ == "__main__":
    unittest.main()
