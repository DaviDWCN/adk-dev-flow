from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.forge_client import GitLabClient
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.okf import validate_repo
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import build_dev_workflow, execute_workflow_run


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=tester@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _extract_node(workflow, name: str):
    for edge in workflow.edges:
        for item in edge:
            if callable(item) and item.__name__ == name:
                return item
            if isinstance(item, (list, tuple)):
                for sub in item:
                    if callable(sub) and sub.__name__ == name:
                        return sub
            if isinstance(item, dict):
                for sub in item.values():
                    if callable(sub) and sub.__name__ == name:
                        return sub
    return None


class TestCoreDataFlowOptimizations(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        (self.repo / "src" / "mypkg").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "mypkg" / "__init__.py").write_text(
            "def solve() -> int:\n    return 0\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "chore: baseline")
        self.store = StateStore(self.repo)
        self.forge = GitLabClient()
        self.service = FlowService(self.repo, self.store, self.forge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_zero_change_escalates_when_ast_context_creates_cpg_cache_in_worktree(self) -> None:
        """execute_worker_node creates worktree/.adk/cache/cpg_cache.db via CodeGraph;
        verify_python_node must still detect a zero-change worker and escalate on round 0.
        """
        shim = self.root / "shim_noop.py"
        shim.write_text(
            "import json, sys\n"
            "_ = sys.stdin.read() if '-p' in sys.argv and sys.argv[-1] == '-p' else ''\n"
            "print(json.dumps({'changed_files': [], 'summary': 'inspected only'}))\n",
            encoding="utf-8",
        )

        run_id = (await self.service.flow_init("Noop worker test"))["run_id"]
        spec = SpecContract(
            title="Zero change with AST cache",
            summary="Must escalate immediately despite .adk/cache/cpg_cache.db",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve() == 42\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return 42", target_files=["src/mypkg/__init__.py"])],
        )
        self.service.flow_lock_spec(run_id, spec, strict_prelock=False)

        env = {
            "ADK_WORKER_PROVIDER": "codebuddy",
            "CODEBUDDY_BIN": sys.executable,
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim)]),
            "CODEBUDDY_USE_STDIN": "1",
            "ADK_TEST_CMD": f'"{sys.executable}" -m pytest -q {{test_path}}',
            "ADK_LINT_CMD": "skip",
            "ADK_MAX_RETRIES": "3",
        }
        with patch.dict(os.environ, env, clear=False):
            await execute_workflow_run(self.repo, self.store, self.forge, run_id)

        run = self.store.get_run(run_id)
        logs = self.store.list_attempt_logs(run_id, limit=10)
        self.assertEqual(run["status"], "ESCALATED")
        self.assertEqual(len(logs), 1, f"Expected immediate round-0 escalation, got {len(logs)} attempts")
        self.assertIn("[zero_change VIOLATION]", logs[0]["combined_err"])

    async def test_repair_hint_and_attempt_logs_use_physical_touched_files_when_cli_returns_prose(self) -> None:
        """When CLI prints plain prose (not JSON), Repair Hint & attempt_logs must record physical git changes."""
        captured_prompts = self.root / "prompts.jsonl"
        shim = self.root / "shim_prose.py"
        shim.write_text(
            f"""import json, pathlib, sys
prompt = sys.stdin.read() if "-p" in sys.argv and sys.argv[-1] == "-p" else sys.argv[-1]
with open({str(captured_prompts)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"prompt": prompt}}, ensure_ascii=False) + "\\n")
target = pathlib.Path.cwd() / "src" / "mypkg" / "__init__.py"
if "第 1/" in prompt:
    target.write_text("def solve() -> int:\\n    return 42\\n", encoding="utf-8")
    print("Fixed solve to return 42.")
else:
    target.write_text("def solve() -> int:\\n    return 1\\n", encoding="utf-8")
    print("Implemented initial version in src/mypkg/__init__.py.")
""",
            encoding="utf-8",
        )

        run_id = (await self.service.flow_init("Prose CLI repair"))["run_id"]
        spec = SpecContract(
            title="Prose CLI repair",
            summary="Verify physical touched_files flow into attempt_logs and Repair Hint",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve() == 42\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return 42", target_files=["src/mypkg/__init__.py"])],
        )
        self.service.flow_lock_spec(run_id, spec, strict_prelock=False)

        env = {
            "ADK_WORKER_PROVIDER": "codebuddy",
            "CODEBUDDY_BIN": sys.executable,
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim)]),
            "CODEBUDDY_USE_STDIN": "1",
            "ADK_TEST_CMD": f'"{sys.executable}" -m pytest -q {{test_path}}',
            "ADK_LINT_CMD": "skip",
            "ADK_ENABLE_CRITIC": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            await execute_workflow_run(self.repo, self.store, self.forge, run_id)

        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "MR_READY", run["last_error_log"])

        conn = sqlite3.connect(str(self.store.db_path))
        conn.row_factory = sqlite3.Row
        logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attempt_logs WHERE run_id = ? ORDER BY attempt ASC", (run_id,)
            ).fetchall()
        ]
        conn.close()

        self.assertEqual(len(logs), 2)
        round0_files = json.loads(logs[0]["changed_files"])
        self.assertIn("src/mypkg/__init__.py", round0_files)

        prompts = [json.loads(line)["prompt"] for line in captured_prompts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(prompts), 2)
        self.assertIn("src/mypkg/__init__.py", prompts[1].split("上一轮修复回执", 1)[1])
        self.assertNotIn("上一轮为空转", prompts[1])

    async def test_okf_spec_roundtrip_preserves_spike_mutation_gate_and_design_doc(self) -> None:
        """flow_lock_spec_from_file must preserve spike, require_mutation_gate, and design_doc."""
        run_id = (await self.service.flow_init("OKF roundtrip"))["run_id"]
        spec = SpecContract(
            issue_iid=42,
            title="OKF full fidelity",
            summary="Ensure spike, mutation flag, and design_doc survive OKF Markdown.",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="def test_solve():\n    assert False\n",
            require_tdd_red=False,
            require_mutation_gate=False,
            spike=True,
            design_doc="## Decisions\nUse deterministic Redis Lua script.",
            tasks=[AtomicTask(id="T1", title="Solve", description="Return 42", target_files=["src/mypkg/__init__.py"])],
        )
        spec_dir = self.repo / ".adk" / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_file = spec_dir / "issue-42.md"
        spec_file.write_text(spec.to_okf_markdown(), encoding="utf-8")

        report = validate_repo(self.repo)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["warnings"], [], f"Unexpected OKF warnings: {report['warnings']}")

        lock_res = self.service.flow_lock_spec_from_file(
            run_id, ".adk/specs/issue-42.md", strict_prelock=False
        )
        self.assertEqual(lock_res["status"], "SPEC_LOCKED")

        stored = SpecContract.model_validate_json(self.store.get_run(run_id)["spec_json"])
        self.assertTrue(stored.spike)
        self.assertFalse(stored.require_mutation_gate)
        self.assertIn("Use deterministic Redis Lua script.", stored.design_doc or "")

    async def test_publish_mr_is_deterministic_without_calling_ai_critic(self) -> None:
        """publish_mr_node uses locked_test_commit_sha..HEAD for deterministic AST gate and never invokes AI Critic."""
        run_id = "critic-base-01"
        self.store.create_run(run_id, issue_iid=12)
        spec = SpecContract(
            issue_iid=12,
            title="Deterministic Step 8",
            summary="Step 8 must rely on AST/DFG gate since locked_test_commit_sha without calling AI Critic",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="def test_solve():\n    assert True\n",
            require_tdd_red=False,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Edit", target_files=["src/mypkg/__init__.py"])],
        )
        self.store.update_run(run_id, status="SPEC_LOCKED", spec_json=spec.model_dump_json())

        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        prep_node = _extract_node(wf, "prepare_worktree_node")
        publish_node = _extract_node(wf, "publish_mr_node")
        prep_out = await prep_node(json.dumps({"spec": spec.model_dump()}))

        self.assertNotEqual(prep_out["base_sha"], prep_out["locked_test_commit_sha"])
        wt = Path(prep_out["worktree"])
        (wt / "src" / "mypkg" / "__init__.py").write_text(
            "def solve() -> int:\n    return 42\n", encoding="utf-8"
        )
        _git(wt, "add", "src/mypkg/__init__.py")
        _git(wt, "commit", "-m", "feat: return 42")
        self.store.update_run(run_id, status="VERIFYING")

        called = False

        async def spy_critic(*, spec, worktree, base_sha, ast_findings, run_id):
            nonlocal called
            called = True
            return {"approved": False, "confidence": 1.0, "summary": "Should not run", "blocking_issues": ["Blocked"]}

        with (
            patch.dict(os.environ, {"ADK_ENABLE_CRITIC": "1", "ADK_CRITIC_FAIL_CLOSED": "1"}),
            patch("adk_dev_flow.workflow.run_readonly_critic_review", side_effect=spy_critic),
        ):
            res = await publish_node(prep_out)

        self.assertFalse(called)
        self.assertEqual(res["status"], "MR_READY")
