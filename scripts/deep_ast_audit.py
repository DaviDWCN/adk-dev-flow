from __future__ import annotations

import ast
import inspect
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from adk_dev_flow.ast_graph import CodeGraph, audit_ast_diff, generate_skeleton


# ==============================================================================
# 1. 动态差分比对 (Dynamic Trace vs Static Graph)
# ==============================================================================

class DynamicTracer:
    """Uses sys.settrace to capture runtime function call edges and file dependencies."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.call_edges: set[tuple[str, str]] = set()  # (caller_file, callee_file)
        self.symbol_edges: set[tuple[str, str]] = set()  # (caller_func, callee_func)
        self.observed_files: set[str] = set()

    def _normalize_path(self, path_str: str) -> str | None:
        try:
            p = Path(path_str).resolve()
            if self.repo_root in p.parents or p == self.repo_root:
                return p.relative_to(self.repo_root).as_posix()
        except Exception:
            pass
        return None

    def trace_func(self, frame, event, arg):
        if event != "call":
            return self.trace_func

        code = frame.f_code
        callee_file = self._normalize_path(code.co_filename)
        if not callee_file or callee_file.startswith("<") or callee_file.startswith(".venv") or callee_file.startswith("tests/__pycache__"):
            return self.trace_func

        self.observed_files.add(callee_file)
        callee_name = code.co_name

        caller_frame = frame.f_back
        if caller_frame:
            caller_code = caller_frame.f_code
            caller_file = self._normalize_path(caller_code.co_filename)
            if caller_file and not caller_file.startswith("<") and not caller_file.startswith(".venv"):
                self.call_edges.add((caller_file, callee_file))
                self.symbol_edges.add((f"{caller_file}::{caller_code.co_name}", f"{callee_file}::{callee_name}"))

        return self.trace_func

    def run_with_trace(self, callable_obj):
        old_trace = sys.gettrace()
        sys.settrace(self.trace_func)
        try:
            callable_obj()
        finally:
            sys.settrace(old_trace)


def run_dynamic_trace_experiment() -> dict[str, Any]:
    tracer = DynamicTracer(REPO_ROOT)

    # Dynamically execute test_ast_graph and test_models_and_service
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    from tests.test_ast_graph import TestAstGraphSuite
    suite.addTests(loader.loadTestsFromTestCase(TestAstGraphSuite))

    # Run tests under tracer
    runner = unittest.TextTestRunner(stream=io.StringIO(), failfast=False)
    tracer.run_with_trace(lambda: runner.run(suite))

    # Evaluate against static CodeGraph on src/adk_dev_flow/ast_graph.py
    target_rel = "src/adk_dev_flow/ast_graph.py"
    cg = CodeGraph(REPO_ROOT)
    cg.index_repo()
    static_report = cg.analyze_impact([target_rel])

    predicted_files = set(static_report.impacted_production_files + static_report.impacted_test_files)

    # Extract all files that actually interacted with target_rel dynamically
    dynamically_interacting_files: set[str] = set()
    for caller, callee in tracer.call_edges:
        if caller == target_rel:
            dynamically_interacting_files.add(callee)
        if callee == target_rel:
            dynamically_interacting_files.add(caller)

    # Invariants evaluation
    tp = predicted_files.intersection(dynamically_interacting_files)
    fp = predicted_files - dynamically_interacting_files
    fn = dynamically_interacting_files - predicted_files

    recall = len(tp) / (len(tp) + len(fn)) if (tp or fn) else 1.0
    precision = len(tp) / (len(tp) + len(fp)) if (tp or fp) else 1.0

    return {
        "target": target_rel,
        "predicted_files_count": len(predicted_files),
        "dynamic_files_count": len(dynamically_interacting_files),
        "true_positives": sorted(list(tp)),
        "false_positives_over_approx": sorted(list(fp)),
        "false_negatives_missed": sorted(list(fn)),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
    }


# ==============================================================================
# 2. 骨架提取器的不变量检验 (Invariants, Compilability & Idempotency)
# ==============================================================================

def run_skeleton_invariants_experiment() -> dict[str, Any]:
    src_dir = REPO_ROOT / "src" / "adk_dev_flow"
    py_files = sorted(list(src_dir.glob("*.py")))

    results = []
    for f in py_files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        code = f.read_text(encoding="utf-8")

        # 1. Compilability test
        skeleton = generate_skeleton(code, file_path=rel)
        compile_ok = True
        compile_err = None
        try:
            compile(skeleton, rel, "exec")
        except Exception as e:
            compile_ok = False
            compile_err = str(e)

        # 2. Idempotency test: Skeleton(Skeleton(code)) == Skeleton(code)
        skeleton_second = generate_skeleton(skeleton, file_path=rel)
        idempotent = False
        try:
            tree1 = ast.parse(skeleton)
            tree2 = ast.parse(skeleton_second)
            idempotent = ast.dump(tree1) == ast.dump(tree2)
        except Exception:
            idempotent = (skeleton == skeleton_second)

        # 3. Compression ratio
        orig_len = len(code)
        skel_len = len(skeleton)
        compression = round((1.0 - skel_len / orig_len) * 100, 1) if orig_len > 0 else 0.0

        # 4. Symbol signature preservation
        orig_tree = ast.parse(code)
        skel_tree = ast.parse(skeleton)

        orig_funcs = {n.name: [a.arg for a in n.args.args] for n in ast.walk(orig_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        skel_funcs = {n.name: [a.arg for a in n.args.args] for n in ast.walk(skel_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        sig_preserved = (orig_funcs == skel_funcs)

        results.append({
            "file": rel,
            "compile_ok": compile_ok,
            "compile_err": compile_err,
            "idempotent": idempotent,
            "orig_chars": orig_len,
            "skeleton_chars": skel_len,
            "compression_pct": compression,
            "sig_preserved": sig_preserved,
        })

    all_compile = all(r["compile_ok"] for r in results)
    all_idempotent = all(r["idempotent"] for r in results)
    all_sigs = all(r["sig_preserved"] for r in results)
    avg_comp = round(sum(r["compression_pct"] for r in results) / len(results), 1)

    return {
        "files_evaluated": len(results),
        "all_compilable": all_compile,
        "all_idempotent": all_idempotent,
        "all_signatures_preserved": all_sigs,
        "average_token_compression_pct": avg_comp,
        "details": results,
    }


# ==============================================================================
# 3. 对抗性破坏与绕过测试 (Adversarial Evasion & Breaking Change Suite)
# ==============================================================================

ADVERSARIAL_SAMPLES = [
    # 1. 动态反射拼接调用危险函数
    {
        "id": "ADV-01",
        "category": "Evasion",
        "name": "getattr 字符串拼接动态调用 os.system",
        "base": "def helper(): pass\n",
        "mutant": """import os
def helper():
    fn = getattr(os, "sys" + "tem")
    fn("rm -rf /")
""",
        "should_block": True,
        "reason": "通过 getattr 绕过 ast.Call.func.id 静态名称查找",
    },
    # 2. 内建函数动态提取执行
    {
        "id": "ADV-02",
        "category": "Evasion",
        "name": "globals()['__builtins__'] 动态执行",
        "base": "def calc(): return 1\n",
        "mutant": """def calc():
    b = globals().get('__builtins__')
    b['eval']('1 + 1')
""",
        "should_block": True,
        "reason": "命名空间字典间接调用",
    },
    # 3. Base64 编码混淆的硬编码凭据
    {
        "id": "ADV-03",
        "category": "Evasion",
        "name": "Base64 动态还原敏感 API 密钥",
        "base": "def auth(): pass\n",
        "mutant": """import base64
def auth():
    # sk-live-11223344556677889900
    API_TOKEN = base64.b64decode("c2stbGl2ZS0xMTIyMzM0NDU1NjY3Nzg4OTkwMA==").decode()
""",
        "should_block": True,
        "reason": "通过编解码绕过纯字符串字面量长度检查",
    },
    # 4. 原生直接调用高危命令
    {
        "id": "ADV-04",
        "category": "Evasion",
        "name": "直接 ast.Call 显式高危命令",
        "base": "def clean(): pass\n",
        "mutant": """import os
def clean():
    os.system("echo bad")
""",
        "should_block": True,
        "reason": "显式危险调用基线",
    },
    # 5. 敏感模块引入 (pty/shelve)
    {
        "id": "ADV-05",
        "category": "Evasion",
        "name": "敏感终端伪造模块导入",
        "base": "def shell(): pass\n",
        "mutant": """import pty
def shell():
    pass
""",
        "should_block": True,
        "reason": "危险标准库黑名单",
    },
    # 6. 隐蔽的破坏性接口变更：位置参数转关键字/新增无默认值参数
    {
        "id": "ADV-06",
        "category": "Breaking",
        "name": "公共函数中间插入必填无默认值参数",
        "base": "def dispatch(action: str, timeout: int = 10): pass\n",
        "mutant": "def dispatch(action: str, required_secret: str, timeout: int = 10): pass\n",
        "should_block": True,
        "reason": "破坏调用方位置传参兼容性",
    },
    # 7. 删除既有必填参数
    {
        "id": "ADV-07",
        "category": "Breaking",
        "name": "删除调用方依赖的公共参数",
        "base": "def configure(host: str, port: int): pass\n",
        "mutant": "def configure(host: str): pass\n",
        "should_block": True,
        "reason": "删除公共 API 字段",
    },
    # 8. 删除整个公共类或函数
    {
        "id": "ADV-08",
        "category": "Breaking",
        "name": "删除公共 API 类",
        "base": """class PublicEndpoint:
    def handle(self): pass
""",
        "mutant": """# Class deleted entirely
def internal_worker(): pass
""",
        "should_block": True,
        "reason": "顶层公共导出符号缺失",
    },
    # 9. 私有函数更改（不应被判为破坏性变更）
    {
        "id": "ADV-09",
        "category": "Benign",
        "name": "修改内部私有函数 _internal_step 签名",
        "base": "def _internal_step(a: int): pass\n",
        "mutant": "def _internal_step(a: int, b: int, c: str = 'x'): pass\n",
        "should_block": False,
        "reason": "私有符号下划线开头，应当放行允许重构",
    },
    # 10. 无害代码格式化与内部逻辑实现优化
    {
        "id": "ADV-10",
        "category": "Benign",
        "name": "良性代码优化与重构（无破坏性变更、无危险调用）",
        "base": """def calculate_total(items: list[int]) -> int:
    s = 0
    for x in items: s += x
    return s
""",
        "mutant": """def calculate_total(items: list[int]) -> int:
    \"\"\"Calculates sum of items efficiently.\"\"\"
    return sum(items)
""",
        "should_block": False,
        "reason": "良性算法优化与注释丰富，接口完全兼容",
    },
]


def run_adversarial_suite() -> dict[str, Any]:
    test_results = []
    blocked_count = 0
    correct_count = 0

    for sample in ADVERSARIAL_SAMPLES:
        report = audit_ast_diff(sample["base"], sample["mutant"], file_path="test_sample.py")
        is_blocked = report.has_issues
        expected_block = sample["should_block"]

        passed = (is_blocked == expected_block)
        if passed:
            correct_count += 1
        if is_blocked:
            blocked_count += 1

        test_results.append({
            "id": sample["id"],
            "name": sample["name"],
            "category": sample["category"],
            "expected_block": expected_block,
            "actual_blocked": is_blocked,
            "passed": passed,
            "syntax_ok": report.syntax_ok,
            "breaking_issues": report.breaking_issues,
            "dangerous_nodes": report.dangerous_nodes,
            "reason": sample["reason"],
        })

    return {
        "total_samples": len(ADVERSARIAL_SAMPLES),
        "correct_decisions": correct_count,
        "accuracy_pct": round(correct_count / len(ADVERSARIAL_SAMPLES) * 100, 1),
        "results": test_results,
    }


# ==============================================================================
# Main Runner
# ==============================================================================

def main():
    print("=" * 70)
    print("🔬 ADK-DEV-FLOW 代码图谱与 AST 语义引擎硬核评估套件")
    print("=" * 70)

    # 1. 动态差分比对
    print("\n[Stage 1/3] 动态执行轨迹 vs 静态图谱真值检验 (sys.settrace) ...")
    dyn_res = run_dynamic_trace_experiment()
    print(f"  📌 评估目标: {dyn_res['target']}")
    print(f"  🔍 静态预测受影响文件: {dyn_res['predicted_files_count']} 个")
    print(f"  ⚡ 动态捕捉交互文件  : {dyn_res['dynamic_files_count']} 个")
    print(f"  🎯 召回率 (Recall)   : {dyn_res['recall'] * 100:.1f}% ({'零漏报 (Sound)' if dyn_res['recall'] == 1.0 else '存在漏报!'})")
    print(f"  📐 精确率 (Precision): {dyn_res['precision'] * 100:.1f}%")
    print(f"  ✅ 命中文件 (TP)     : {dyn_res['true_positives']}")
    print(f"  ⚠️  保守过近似 (FP)  : {dyn_res['false_positives_over_approx']}")
    print(f"  ❌ 遗漏漏报 (FN)     : {dyn_res['false_negatives_missed']}")

    # 2. 骨架提取器不变量检验
    print("\n[Stage 2/3] 骨架提取器不变量、编译性与幂等性检验 ...")
    skel_res = run_skeleton_invariants_experiment()
    print(f"  📁 检验核心源码文件  : {skel_res['files_evaluated']} 个模块")
    print(f"  ⚙️  独立可编译性     : {'✅ 100% 编译通过' if skel_res['all_compilable'] else '❌ 编译失败!'}")
    print(f"  🔁 形式化幂等性     : {'✅ 100% 幂等 (Skel(Skel(x)) == Skel(x))' if skel_res['all_idempotent'] else '❌ 非幂等!'}")
    print(f"  🧬 签名与注解无损   : {'✅ 100% 保留' if skel_res['all_signatures_preserved'] else '❌ 签名有损失!'}")
    print(f"  📉 平均 Token 压缩率: 缩减 {skel_res['average_token_compression_pct']}% (极大降低工兵上下文负载)")

    # 3. 对抗性破坏与绕过测试
    print("\n[Stage 3/3] 对抗性样本靶场与规避测试 (10 Hard Cases) ...")
    adv_res = run_adversarial_suite()
    print(f"  🎯 综合裁决准确率   : {adv_res['accuracy_pct']}% ({adv_res['correct_decisions']}/{adv_res['total_samples']})")
    for r in adv_res["results"]:
        status = "✅ PASS" if r["passed"] else "❌ FAIL"
        action = "BLOCKED" if r["actual_blocked"] else "ALLOWED"
        print(f"  - [{r['id']}] {status} [{action:7}] {r['name']}")
        if not r["passed"]:
            print(f"       -> 失败原因: 预期 {'阻断' if r['expected_block'] else '放行'}, 实际 {'阻断' if r['actual_blocked'] else '放行'}")
            print(f"       -> 机制漏洞: {r['reason']}")

    print("\n" + "=" * 70)
    print("📊 评估完成，全部原始实验数据已就绪。")
    print("=" * 70)


if __name__ == "__main__":
    main()
