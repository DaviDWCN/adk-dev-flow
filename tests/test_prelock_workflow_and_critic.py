from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.dashboard import _build_run_payload
from adk_dev_flow.forge_client import GitLabClient
from adk_dev_flow.mcp_server import FlowService, create_mcp_server
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore, lifecycle_highlight_critic, lifecycle_step
from adk_dev_flow.workflow import build_dev_workflow


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


class TestPreLockWorkflowAndCriticShift(unittest.IsolatedAsyncioTestCase):
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
            "def solve(x: int) -> int:\n    return 0\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "chore: baseline")
        self.store = StateStore(self.repo)
        self.forge = GitLabClient()
        self.service = FlowService(self.repo, self.store, self.forge)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_flow_preflight_runs_in_isolated_worktree_without_dirtying_main_repo(self) -> None:
        """flow_preflight_test must execute in a clean temp worktree (not write to main repo) and persist red_evidence."""
        run_id = (await self.service.flow_init("Isolated preflight"))["run_id"]
        test_rel = "tests/test_isolated_preflight.py"
        # Embed an assertion that checks whether CWD is NOT self.repo
        test_code = (
            "from pathlib import Path\n"
            f"MAIN_REPO = {str(self.repo)!r}\n"
            "def test_isolated():\n"
            "    assert str(Path.cwd().resolve()) != MAIN_REPO\n"
            "    assert False, 'business assertion red'\n"
        )
        res = self.service.flow_preflight_test(test_rel, test_code, run_id=run_id)
        self.assertTrue(res["ok"], f"Preflight must run outside main repo worktree: {res}")
        self.assertFalse((self.repo / test_rel).exists(), "Main repo must never be dirtied by preflight")
        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "TEST_GENERATED")
        prelock = json.loads(run.get("prelock_json") or "{}")
        self.assertIn("red_evidence", prelock)
        self.assertEqual(prelock["red_evidence"].get("test_path"), test_rel)
        self.assertTrue(prelock["red_evidence"].get("test_sha256"))

    async def test_flow_pre_critic_runs_at_step3_and_gates_spec_lock(self) -> None:
        """Step 3 flow_pre_critic evaluates design+test via read-only CodeBuddyLlm and controls lock eligibility."""
        shim = self.root / "shim_precritic.py"
        shim.write_text(
            """import json, os, sys
_ = sys.stdin.read()
mode = os.environ.get("PRECRITIC_MODE", "reject")
if mode == "reject":
    print(json.dumps({
        "approved": False,
        "consistency_score": 0.4,
        "requirement_coverage": ["missing error case"],
        "anti_cheat_risk": "high: single constant test can be hardcoded",
        "blocking_issues": ["Weak assertion in test_solve"],
        "summary": "Rejected weak test contract",
    }))
else:
    print(json.dumps({
        "approved": True,
        "consistency_score": 0.95,
        "requirement_coverage": ["happy path", "boundary"],
        "anti_cheat_risk": "low",
        "blocking_issues": [],
        "summary": "Design and tests cover all requirements cleanly",
    }))
""",
            encoding="utf-8",
        )
        run_id = (await self.service.flow_init("Step 3 Pre-Critic gate"))["run_id"]
        spec = SpecContract(
            title="Solve feature",
            summary="Solve returns x * 2",
            design_doc="Multiply input by 2.",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve(2) == 4\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )

        impact_res = self.service.flow_analyze_impact(["src/mypkg/__init__.py"], run_id=run_id)
        self.assertEqual(impact_res.get("status"), "SPEC_DESIGNED")
        red_res = self.service.flow_preflight_test(spec.acceptance_test_path, spec.acceptance_test_code, run_id=run_id)
        self.assertTrue(red_res["ok"], red_res)

        env = {
            "ADK_WORKER_PROVIDER": "codebuddy",
            "CODEBUDDY_BIN": sys.executable,
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim)]),
            "PRECRITIC_MODE": "reject",
        }
        with patch.dict(os.environ, env, clear=False):
            pc_rej = await self.service.flow_pre_critic(run_id=run_id, spec=spec)
        self.assertFalse(pc_rej["approved"])
        self.assertEqual(self.store.get_run(run_id)["status"], "TEST_GENERATED")

        # Cannot lock while Pre-Critic rejected
        lock_blocked = self.service.flow_lock_spec(run_id, spec)
        self.assertIn("error", lock_blocked)

        # Now revise and approve in Step 3
        with patch.dict(os.environ, {**env, "PRECRITIC_MODE": "approve"}, clear=False):
            pc_ok = await self.service.flow_pre_critic(run_id=run_id, spec=spec)
        self.assertTrue(pc_ok["approved"])
        self.assertEqual(self.store.get_run(run_id)["status"], "PRE_CRITIC_REVIEWED")

        lock_ok = self.service.flow_lock_spec(run_id, spec)
        self.assertEqual(lock_ok["status"], "SPEC_LOCKED", lock_ok)
        stored = SpecContract.model_validate_json(self.store.get_run(run_id)["spec_json"])
        self.assertIsNotNone(stored.red_evidence)
        self.assertIsNotNone(stored.pre_critic_report)
        self.assertIsNotNone(stored.impact_analysis)

    async def test_flow_lock_spec_rejects_missing_prelock_and_test_tampering_after_red(self) -> None:
        """MCP flow_lock_spec must refuse skipping Pre-Lock AND refuse swapped test code after preflight."""
        mcp, service = create_mcp_server(repo_root=self.repo, store=self.store, forge=self.forge)
        run_id = (await service.flow_init("Enforce strict prelock"))["run_id"]

        spec = SpecContract(
            title="Strict gate",
            summary="Cannot jump SPEC_DRAFTING -> SPEC_LOCKED",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve(2) == 4\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )

        # Find registered MCP tool function for flow_lock_spec
        lock_tool = self.service.flow_lock_spec(run_id, spec)
        self.assertIn("error", lock_tool, "Calling flow_lock_spec directly from SPEC_DRAFTING must be blocked")

        # Now pass preflight with test A and pre_critic, then swap test code to B in lock!
        service.flow_analyze_impact(["src/mypkg/__init__.py"], run_id=run_id)
        service.flow_preflight_test(spec.acceptance_test_path, spec.acceptance_test_code, run_id=run_id)
        await service.flow_pre_critic(run_id=run_id, spec=spec)

        tampered_spec = spec.model_copy(
            update={"acceptance_test_code": "def test_solve():\n    assert True\n"}
        )
        tamper_res = service.flow_lock_spec(run_id, tampered_spec)
        self.assertIn("error", tamper_res, "Swapping acceptance_test_code after preflight must be blocked")

    async def test_build_prelock_workflow_runs_as_adk_workflow_and_stops_at_hitl(self) -> None:
        """execute_prelock_workflow must run Pre-Lock inside Google ADK Workflow and halt at Step 3 (PRE_CRITIC_REVIEWED)."""
        from adk_dev_flow.workflow import execute_prelock_workflow

        run_id = (await self.service.flow_init("ADK Pre-Lock graph"))["run_id"]
        spec = SpecContract(
            title="ADK Pre-Lock",
            summary="Run impact -> isolated red preflight -> step 3 critic via ADK Workflow",
            design_doc="Implement solve(x) = x * 2",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve(2) == 4\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )

        with patch.dict(os.environ, {"ADK_WORKER_PROVIDER": "mock"}, clear=False):
            result = await execute_prelock_workflow(self.repo, self.store, run_id=run_id, spec=spec)
        self.assertEqual(result["status"], "PRE_CRITIC_REVIEWED")
        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "PRE_CRITIC_REVIEWED")
        payload = _build_run_payload(run, "repo")
        self.assertEqual(payload["step"], 3)
        self.assertTrue(payload["highlight_critic"])
        self.assertIsNotNone(payload["impact_analysis"])
        self.assertIsNotNone(payload["red_evidence"])
        self.assertIsNotNone(payload["pre_critic_report"])

    async def test_step8_publish_mr_is_100pct_deterministic_and_ignores_llm_critic(self) -> None:
        """Step 8 publish_mr_node must be purely deterministic (AST+DFG+symbol) and NEVER invoke AI Critic."""
        self.assertFalse(
            lifecycle_highlight_critic("REVIEWING"),
            "Step 8 REVIEWING is deterministic AST gatekeeper; AI Critic highlight belongs to Step 3 PRE_CRITIC_REVIEWED only",
        )
        self.assertTrue(lifecycle_highlight_critic("PRE_CRITIC_REVIEWED"))

        run_id = "det-step8-01"
        self.store.create_run(run_id, issue_iid=15)
        spec = SpecContract(
            issue_iid=15,
            title="Deterministic Step 8",
            summary="No AI Critic uncertainty in Step 8",
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

        wt = Path(prep_out["worktree"])
        (wt / "src" / "mypkg" / "__init__.py").write_text(
            "def solve(x: int) -> int:\n    return x * 2\n", encoding="utf-8"
        )
        _git(wt, "add", "src/mypkg/__init__.py")
        _git(wt, "commit", "-m", "feat: return x*2")
        self.store.update_run(run_id, status="VERIFYING")

        called = False

        async def fail_if_called(*args, **kwargs):
            nonlocal called
            called = True
            return {"approved": False, "blocking_issues": ["LLM blocked"], "summary": "No"}

        with (
            patch.dict(os.environ, {"ADK_ENABLE_CRITIC": "1", "ADK_CRITIC_FAIL_CLOSED": "1"}),
            patch("adk_dev_flow.workflow.run_readonly_critic_review", side_effect=fail_if_called),
        ):
            res = await publish_node(prep_out)

        self.assertFalse(called, "Step 8 publish_mr_node must NOT call AI Critic")
        self.assertEqual(res["status"], "MR_READY")
        self.assertEqual(lifecycle_step("PRE_CRITIC_REVIEWED"), 3)

    async def test_post_green_mutation_gate_catches_cheating_worker_when_tdd_red_true(self) -> None:
        """When ADK_POST_GREEN_MUTATION=1 and tests pass under require_tdd_red=True,
        verify_python_node must deterministically run mutation checks on changed .py files.
        """
        from adk_dev_flow.workflow import execute_workflow_run

        shim = self.root / "shim_cheat.py"
        shim.write_text(
            """import json, pathlib
target = pathlib.Path.cwd() / "src" / "mypkg" / "__init__.py"
# Cheating / poorly tested logic: passes test_solve(2)==4, but dead arithmetic y = x + 100 survives mutation!
target.write_text("def solve(x: int) -> int:\\n    _unused = x + 100\\n    if x == 2:\\n        return 4\\n    return 0\\n", encoding="utf-8")
print(json.dumps({"changed_files": ["src/mypkg/__init__.py"], "summary": "implemented"}))
""",
            encoding="utf-8",
        )

        run_id = (await self.service.flow_init("Post-green mutation gate"))["run_id"]
        spec = SpecContract(
            title="Post-green mutation check",
            summary="Catch dead logic / weak test after tests turn green",
            design_doc="Compute solve(x) = x * 2",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code="from src.mypkg import solve\ndef test_solve():\n    assert solve(2) == 4\n",
            require_tdd_red=True,
            require_mutation_gate=True,
            tasks=[AtomicTask(id="T1", title="Solve", description="Return x*2", target_files=["src/mypkg/__init__.py"])],
        )
        self.service.flow_lock_spec(run_id, spec, strict_prelock=False)

        env = {
            "ADK_WORKER_PROVIDER": "codebuddy",
            "CODEBUDDY_BIN": sys.executable,
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim)]),
            "CODEBUDDY_USE_STDIN": "1",
            "ADK_TEST_CMD": f'"{sys.executable}" -m pytest -q {{test_path}}',
            "ADK_LINT_CMD": "skip",
            "ADK_POST_GREEN_MUTATION": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            await execute_workflow_run(self.repo, self.store, self.forge, run_id)

        run = self.store.get_run(run_id)
        self.assertEqual(run["status"], "ESCALATED")
        self.assertIn("变异测试", run["last_error_log"])
