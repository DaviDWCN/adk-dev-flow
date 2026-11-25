from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from adk_dev_flow.ast_graph import audit_ast_diff
from adk_dev_flow.forge_client import BaseGitForge
from adk_dev_flow.models import AtomicTask, ReviewVerdict, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.workflow import (
    _validate_tdd_red_outcome,
    build_dev_workflow,
    enforce_deterministic_verdict_guard,
)


class DummyForge(BaseGitForge):
    @property
    def platform_name(self) -> str:
        return "dummy"

    @property
    def enabled(self) -> bool:
        return False

    def get_issue(self, issue_iid: int) -> dict[str, Any]:
        return {"iid": issue_iid, "title": "Test", "description": "Desc"}

    def create_or_update_mr(self, **kwargs) -> dict[str, Any]:
        return {"iid": 1, "web_url": "https://example.com/mr/1"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        pass

    def mark_mr_ready(self, mr_iid: int) -> dict[str, Any]:
        return {"iid": mr_iid, "ready": True}


class TestAdversarialWorkflowBenchmark(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / ".git").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        self.store = StateStore(self.repo / "state.db")
        self.forge = DummyForge()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_adversary_1_critic_hallucination_override_blocks_mr_ready(self) -> None:
        """Adversary 1: Critic LLM hallucinates approved=True despite severe AST breaking changes.

        Safety Invariant 1: System MUST fail-closed; status MUST NOT become MR_READY!
        """
        run_id = "adv-run-001"
        self.store.create_run(run_id, issue_iid=1)

        spec = SpecContract(
            issue_iid=1,
            title="Adversarial Critic Bypass",
            summary="Attack test",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_x.py",
            acceptance_test_code="def test_x(): assert True\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Task 1",
                    description="Desc",
                    target_files=["src/service.py"],
                )
            ],
        )
        self.store.update_run(
            run_id,
            status="REVIEWING",
            spec_json=spec.model_dump_json(),
            worktree_path=str(self.repo),
            branch="feat/adv-1",
        )

        # Build workflow
        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)

        # Node index 2 in 3rd pipeline stage is publish_mr_node
        # Let's find publish_mr_node from workflow
        publish_mr_node = None
        for edge in wf.edges:
            # edge is tuple of nodes
            for item in edge:
                if callable(item) and item.__name__ == "publish_mr_node":
                    publish_mr_node = item
                elif isinstance(item, (list, tuple)):
                    for sub in item:
                        if callable(sub) and sub.__name__ == "publish_mr_node":
                            publish_mr_node = sub
                elif isinstance(item, dict):
                    for sub in item.values():
                        if callable(sub) and sub.__name__ == "publish_mr_node":
                            publish_mr_node = sub

        self.assertIsNotNone(publish_mr_node, "publish_mr_node must exist in workflow")

        # Simulate Critic hallucination input: Critic LLM returns approved=True
        # BUT AST findings has breaking changes and dangerous nodes
        fake_ast_finding = {
            "file_path": "src/service.py",
            "syntax_ok": True,
            "breaking_issues": ["符号 `process` 移除了参数 `data` (破坏向后兼容性)"],
            "dangerous_nodes": ["发现危险函数调用 `eval` (line 12)"],
            "has_issues": True,
        }

        # Mock hallucinated critic payload
        hallucinated_critic_output = {
            "approved": True,
            "confidence": 0.99,
            "summary": "Everything looks perfect to me, approved!",
            "blocking_issues": [],
            "ast_findings": [fake_ast_finding],
        }

        # We execute publish_mr_node
        res = await publish_mr_node(hallucinated_critic_output)

        # Invariant check:
        # 1. Returned status must NOT be MR_READY (must be ESCALATED)
        self.assertEqual(res.get("status"), "ESCALATED")
        # 2. Database record must be ESCALATED
        run_record = self.store.get_run(run_id)
        self.assertIsNotNone(run_record)
        self.assertEqual(run_record["status"], "ESCALATED")
        # 3. Approved must be False due to deterministic guard
        self.assertFalse(res.get("review", {}).get("approved", True))

    def test_adversary_2_mock_pass_test_intercepted_at_preflight(self) -> None:
        """Adversary 2: Cheating/empty test passes on baseline without testing new functionality.

        Safety Invariant: TDD Red check must reject exit=0 before implementation code is written.
        """
        # If pytest exit code is 0 on baseline, it MUST raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            _validate_tdd_red_outcome(
                returncode=0,
                combined_output="1 passed in 0.01s",
                test_path="tests/test_cheating.py",
            )
        self.assertIn("TDD 预检失败", str(ctx.exception))
        self.assertIn("未编写业务代码前直接通过", str(ctx.exception))

    def test_adversary_3_closure_trojan_detected_in_nested_scope(self) -> None:
        """Adversary 3: Dangerous execution buried deep inside closure / nested callback.

        Safety Invariant: Static AST auditor must recurse through all lexical scopes.
        """
        code = """
def outer_service_factory():
    secret_key = "dummy"
    def inner_callback():
        def deeply_nested_trojan():
            eval("os.system('id')")
        return deeply_nested_trojan
    return inner_callback
"""
        report = audit_ast_diff("", code, file_path="src/trojan.py")
        self.assertTrue(report.has_issues)
        self.assertTrue(any("eval" in d for d in report.dangerous_nodes))

    async def test_adversary_4_infinite_repair_loop_terminates_strictly_at_max_retries(self) -> None:
        """Adversary 4: Infinite livelock when repair cycle repeatedly fails with different logs.

        Liveness Invariant: Finite termination guaranteed when attempt exceeds MAX_REPAIR_ATTEMPTS.
        """
        run_id = "adv-run-004"
        self.store.create_run(run_id, issue_iid=4)
        spec = SpecContract(
            issue_iid=4,
            title="Livelock prevention",
            summary="Test",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_loop.py",
            acceptance_test_code="def test_l(): assert False\n",
            tasks=[AtomicTask(id="T1", title="T1", description="D", target_files=["src/x.py"])],
        )

        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        verify_node = None
        for edge in wf.edges:
            for item in edge:
                if callable(item) and item.__name__ == "verify_python_node":
                    verify_node = item

        self.assertIsNotNone(verify_node)

        # Attempt = 4 (which exceeds MAX_REPAIR_ATTEMPTS = 3)
        input_data = {
            "spec": spec.model_dump(),
            "worktree": str(self.repo),
            "attempt": 4,
            "last_error_log": "Different error at attempt 4",
        }

        # Mock test runner failing
        with patch("adk_dev_flow.workflow.run_subprocess_safe") as mock_sub:
            from adk_dev_flow.subproc import CommandResult
            mock_sub.return_value = CommandResult(returncode=1, stdout="Failed test", stderr="")
            event = await verify_node(input_data)
            self.assertEqual(event.actions.route, "escalated")
            self.assertIn("最大自动修复轮次上限", event.output.get("escalation_reason", ""))

    async def test_adversary_5_ntfs_handle_contention_and_cas_concurrency(self) -> None:
        """Adversary 5: NTFS handle contention, stale file locks, and concurrent CAS state races.

        Robustness Invariant:
        1. Stale git locks must be safely cleaned up without crashing on held handles.
        2. Simultaneous CAS state transitions must remain strictly mutually exclusive without corrupting SQLite.
        """
        from adk_dev_flow.worktree_utils import sanitize_worktree_state

        # 1. Stale git index.lock cleanup
        index_lock = self.repo / ".git" / "index.lock"
        index_lock.write_text("dummy-lock-pid-9999", encoding="utf-8")
        self.assertTrue(index_lock.exists())

        await sanitize_worktree_state(self.repo)
        self.assertFalse(index_lock.exists(), "Stale git index.lock must be cleaned up")

        # 2. Concurrent CAS state updates on the same run
        run_id = "adv-run-005"
        self.store.create_run(run_id, issue_iid=5)
        self.store.update_run(run_id, status="RUNNING")

        # Two competing operations attempt to transition RUNNING -> VERIFYING vs RUNNING -> ESCALATED
        res_a = self.store.transition_run(run_id, expected_statuses=["RUNNING"], new_status="VERIFYING")
        res_b = self.store.transition_run(run_id, expected_statuses=["RUNNING"], new_status="ESCALATED")

        # Exactly one CAS transition must succeed
        self.assertTrue(res_a ^ res_b, "Exactly one CAS transition must succeed")
        run_record = self.store.get_run(run_id)
        self.assertIsNotNone(run_record)
        self.assertIn(run_record["status"], {"VERIFYING", "ESCALATED"})

    async def test_adversary_6_zero_change_worker_escalates_immediately(self) -> None:
        """Adversary 6: Worker produces zero changes against its frozen baseline.

        Liveness Invariant: a no-op worktree must escalate immediately instead of
        burning MAX_REPAIR_ATTEMPTS on identical empty outcomes. Real-world lesson:
        run 558a138f54eb spent all 3 repair rounds on zero-change worktrees.
        """
        import hashlib
        import subprocess as sp

        tmp = tempfile.TemporaryDirectory()
        try:
            repo = Path(tmp.name) / "repo"
            store_root = Path(tmp.name) / "store-root"
            repo.mkdir()
            store_root.mkdir()

            def git(*args: str) -> sp.CompletedProcess:
                return sp.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

            git("init", "-b", "main")
            git("config", "user.name", "Tester")
            git("config", "user.email", "tester@example.com")
            (repo / "tests").mkdir()
            test_file = repo / "tests" / "test_app.py"
            test_file.write_text("def test_x():\n    assert False\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "chore: baseline")
            base_sha = git("rev-parse", "HEAD").stdout.strip()
            locked_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

            store = StateStore(store_root / "state.db")
            run_id = "adv-run-006"
            store.create_run(run_id, issue_iid=6)

            spec = SpecContract(
                issue_iid=6,
                title="Zero-output worker",
                summary="Worker never writes anything",
                allowed_paths=["src/"],
                acceptance_test_path="tests/test_app.py",
                acceptance_test_code="def test_x():\n    assert False\n",
                tasks=[AtomicTask(id="T1", title="T1", description="D", target_files=["src/x.py"])],
            )

            wf = build_dev_workflow(repo, store, self.forge, run_id=run_id)
            verify_node = None
            for edge in wf.edges:
                for item in edge:
                    if callable(item) and item.__name__ == "verify_python_node":
                        verify_node = item
            self.assertIsNotNone(verify_node)

            node_input = {
                "spec": spec.model_dump(),
                "worktree": str(repo),
                "attempt": 0,
                "base_sha": base_sha,
                "locked_test_sha256": locked_sha,
                "locked_test_commit_sha": base_sha,
            }

            # Real git via run_subprocess_safe; lint/pytest replaced with canned
            # outcomes (no venv/ruff in the sandbox repo).
            real_run = run_subprocess_safe
            from adk_dev_flow.subproc import CommandResult

            async def smart_run(cmd, **kw):
                if cmd and cmd[0] == "git":
                    return await real_run(cmd, **kw)
                if cmd and cmd[0] == "ruff":
                    return CommandResult(returncode=0, stdout="", stderr="")
                return CommandResult(
                    returncode=1,
                    stdout="ImportError: cannot import name 'runtime_bridge' from 'uwra.api'\n5 failed in 0.87s",
                    stderr="",
                )

            with patch("adk_dev_flow.workflow.run_subprocess_safe", smart_run), patch(
                "adk_dev_flow.worktree_utils.run_subprocess_safe", smart_run
            ):
                event = await verify_node(node_input)

            self.assertEqual(event.actions.route, "escalated")
            self.assertIn("零改动", event.output.get("escalation_reason", ""))

            run = store.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertIn("[zero_change VIOLATION]", run["last_error_log"])
            self.assertTrue(run["last_error_sig"])

            # The failed round must be persisted as per-attempt evidence
            history = store.list_attempt_logs(run_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["attempt"], 0)
            self.assertEqual(history[0]["err_sig"], run["last_error_sig"])
            self.assertIn("[zero_change VIOLATION]", history[0]["combined_err"])
        finally:
            tmp.cleanup()

    async def test_adversary_7_repair_hint_feeds_back_worker_receipt(self) -> None:
        """Adversary 7: Repair rounds run blind because the worker output is never fed back.

        Continuity Invariant: each repair prompt must carry the previous round's
        worker summary / changed files (from memory, or persisted attempt_logs),
        and explicitly flag a zero-output previous round (lesson: run 558a138f54eb).
        """
        from unittest.mock import patch as sync_patch

        from adk_dev_flow.providers import MockWorkerProvider

        run_id = "adv-run-007"
        self.store.create_run(run_id, issue_iid=7)
        spec = SpecContract(
            issue_iid=7,
            title="Blind repair round",
            summary="Test",
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_hint.py",
            acceptance_test_code="def test_h(): assert False\n",
            tasks=[AtomicTask(id="T1", title="T1", description="D", target_files=["src/x.py"])],
        )

        wf = build_dev_workflow(self.repo, self.store, self.forge, run_id=run_id)
        worker_node = None
        for edge in wf.edges:
            for item in edge:
                if callable(item) and item.__name__ == "execute_worker_node":
                    worker_node = item
        self.assertIsNotNone(worker_node)

        captured: dict = {}

        class RecorderProvider(MockWorkerProvider):
            async def run_worker(self, *, worktree, prompt, **kwargs):
                captured["prompt"] = prompt
                return {"changed_files": ["src/x.py"], "summary": "fresh round ran"}

        def fake_provider(*args, **kwargs):
            return RecorderProvider()

        # Case 1: in-memory continuity — previous round claimed exploration, wrote nothing
        with sync_patch("adk_dev_flow.workflow.get_worker_provider", fake_provider):
            await worker_node(
                {
                    "spec": spec.model_dump(),
                    "worktree": str(self.repo),
                    "attempt": 1,
                    "last_error_log": "[pytest exit=1]\nImportError: boom",
                    "worker_summary": "explored only, no write",
                    "worker_changed_files": [],
                }
            )
        self.assertIn("上一轮修复回执", captured["prompt"])
        self.assertIn("上一轮工兵自述: explored only, no write", captured["prompt"])
        self.assertIn("上一轮实际改动文件: 无", captured["prompt"])

        # Case 2: store fallback — node_input carries no worker fields, evidence
        # comes from the persisted attempt_logs (cross-session resume)
        self.store.log_attempt(run_id, 1, worker_summary="from store", changed_files=["src/y.py"])
        captured.clear()
        with sync_patch("adk_dev_flow.workflow.get_worker_provider", fake_provider):
            await worker_node(
                {
                    "spec": spec.model_dump(),
                    "worktree": str(self.repo),
                    "attempt": 2,
                    "last_error_log": "[pytest exit=1]\nImportError: boom",
                }
            )
        self.assertIn("上一轮工兵自述: from store", captured["prompt"])
        self.assertIn("src/y.py", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
