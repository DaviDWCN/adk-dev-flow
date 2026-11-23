from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.gitlab_client import GitLabClient
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, ReviewVerdict, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.workflow import _load_okf_rules


class TestOKFIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sample_spec = SpecContract(
            issue_iid=42,
            title="feat: add distributed lock",
            summary="Implement redis distributed lock with automatic TTL renewal.",
            target_branch="main",
            allowed_paths=["src/lock", "src/utils"],
            acceptance_test_path="tests/test_lock.py",
            acceptance_test_code=(
                "import pytest\n\n"
                "def test_lock_acquire():\n"
                "    assert True\n"
            ),
            require_tdd_red=True,
            extra_dependencies=["redis>=5.0.0"],
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Implement core lock",
                    description="Create RedisLock class",
                    target_files=["src/lock/redis.py"],
                ),
                AtomicTask(
                    id="T2",
                    title="Add utility helpers",
                    description="Helper functions for TTL parsing",
                    target_files=["src/utils/time.py"],
                ),
            ],
        )

    def test_spec_contract_okf_roundtrip(self) -> None:
        okf_md = self.sample_spec.to_okf_markdown()
        self.assertIn("type: DevSpec", okf_md)
        self.assertIn("feat: add distributed lock", okf_md)
        self.assertIn("require_tdd_red: true", okf_md)
        self.assertIn("```python", okf_md)
        self.assertIn("def test_lock_acquire():", okf_md)

        # Parse back
        loaded = SpecContract.from_okf_markdown(okf_md)
        self.assertEqual(loaded.issue_iid, 42)
        self.assertEqual(loaded.title, self.sample_spec.title)
        self.assertEqual(loaded.summary, self.sample_spec.summary)
        self.assertEqual(loaded.target_branch, "main")
        self.assertEqual(loaded.allowed_paths, ["src/lock", "src/utils"])
        self.assertEqual(loaded.acceptance_test_path, "tests/test_lock.py")
        self.assertEqual(loaded.acceptance_test_code.strip(), self.sample_spec.acceptance_test_code.strip())
        self.assertTrue(loaded.require_tdd_red)
        self.assertEqual(loaded.extra_dependencies, ["redis>=5.0.0"])
        self.assertEqual(len(loaded.tasks), 2)
        self.assertEqual(loaded.tasks[0].id, "T1")
        self.assertEqual(loaded.tasks[1].target_files, ["src/utils/time.py"])

    def test_spec_contract_from_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_file = Path(tmp) / "issue-42.md"
            spec_file.write_text(self.sample_spec.to_okf_markdown(), encoding="utf-8")

            loaded = SpecContract.from_okf_markdown(spec_file)
            self.assertEqual(loaded.title, self.sample_spec.title)
            self.assertEqual(loaded.issue_iid, 42)

    def test_spec_contract_okf_validation_errors(self) -> None:
        # 1. Missing frontmatter
        with self.assertRaises(ValueError) as ctx:
            SpecContract.from_okf_markdown("# Just Markdown\nNo frontmatter")
        self.assertIn("缺少以 '---' 包裹的 YAML Frontmatter", str(ctx.exception))

        # 2. Wrong type
        bad_type = (
            "---\n"
            "type: OtherDoc\n"
            "title: Foo\n"
            "---\n"
            "```python\nassert True\n```"
        )
        with self.assertRaises(ValueError) as ctx:
            SpecContract.from_okf_markdown(bad_type)
        self.assertIn("expected 'DevSpec'", str(ctx.exception))

        # 3. Missing acceptance test code block
        no_code = (
            "---\n"
            "type: DevSpec\n"
            "title: Foo\n"
            "allowed_paths: ['src']\n"
            "acceptance_test_path: 'tests/test_foo.py'\n"
            "tasks:\n"
            "  - id: T1\n"
            "    title: Task 1\n"
            "    description: desc\n"
            "    target_files: ['src/a.py']\n"
            "---\n"
            "# No python code here"
        )
        with self.assertRaises(ValueError) as ctx:
            SpecContract.from_okf_markdown(no_code)
        self.assertIn("未找到有效的 pytest 验收测试代码块", str(ctx.exception))

        # 4. Syntax error in python acceptance code
        bad_syntax = (
            "---\n"
            "type: DevSpec\n"
            "title: Foo\n"
            "allowed_paths: ['src']\n"
            "acceptance_test_path: 'tests/test_foo.py'\n"
            "tasks:\n"
            "  - id: T1\n"
            "    title: Task 1\n"
            "    description: desc\n"
            "    target_files: ['src/a.py']\n"
            "---\n"
            "```python\ndef broken syntax(\n```"
        )
        with self.assertRaises(ValueError) as ctx:
            SpecContract.from_okf_markdown(bad_syntax)
        self.assertIn("Python 语法错误", str(ctx.exception))

    def test_review_verdict_okf_roundtrip(self) -> None:
        verdict = ReviewVerdict(
            approved=True,
            blocking_issues=[],
            summary="All acceptance tests and security checks passed cleanly.",
        )
        provenance = {
            "base_sha": "abc1234",
            "locked_test_commit_sha": "def5678",
            "generator": "adk-dev-flow:critic",
        }
        okf_md = verdict.to_okf_markdown(
            run_id="run-test-01",
            provenance=provenance,
            raw_report="### Report\nEverything looks good.",
        )
        self.assertIn("type: ReviewVerdict", okf_md)
        self.assertIn("run_id: run-test-01", okf_md)
        self.assertIn("verified: process:acceptance_gate_passed", okf_md)
        self.assertIn("status: passed", okf_md)
        self.assertIn("### Report", okf_md)

        # Parse back
        loaded = ReviewVerdict.from_okf_markdown(okf_md)
        self.assertTrue(loaded.approved)
        self.assertEqual(loaded.summary, verdict.summary)
        self.assertEqual(loaded.blocking_issues, [])

        # Test rejected verdict
        rej_verdict = ReviewVerdict(
            approved=False,
            blocking_issues=["Memory leak detected in connection pool"],
            summary="Rejected due to leak.",
        )
        rej_md = rej_verdict.to_okf_markdown("run-test-02")
        self.assertIn("verified: process:acceptance_gate_failed", rej_md)
        self.assertIn("status: rejected", rej_md)
        loaded_rej = ReviewVerdict.from_okf_markdown(rej_md)
        self.assertFalse(loaded_rej.approved)
        self.assertEqual(loaded_rej.blocking_issues, ["Memory leak detected in connection pool"])

    async def test_flow_lock_spec_from_file_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / ".git").mkdir()
            store = StateStore(repo_root)
            gl = GitLabClient()
            service = FlowService(repo_root, store, gl)

            # Init run
            init_res = await service.flow_init(requirement="Implement caching")
            run_id = init_res["run_id"]

            # Write OKF spec file in repo
            specs_dir = repo_root / ".adk" / "specs"
            specs_dir.mkdir(parents=True, exist_ok=True)
            spec_path = specs_dir / "cache_spec.md"
            spec_path.write_text(self.sample_spec.to_okf_markdown(), encoding="utf-8")

            # Lock from file
            lock_res = service.flow_lock_spec_from_file(run_id, ".adk/specs/cache_spec.md")
            self.assertEqual(lock_res["status"], "SPEC_LOCKED")

            # Verify in store
            run = store.get_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run["status"], "SPEC_LOCKED")
            locked_spec = SpecContract.model_validate_json(run["spec_json"])
            self.assertEqual(locked_spec.title, self.sample_spec.title)

            # Non-existent file returns error
            err_res = service.flow_lock_spec_from_file(run_id, ".adk/specs/non_existent.md")
            self.assertIn("error", err_res)
            self.assertIn("未找到", err_res["error"])

    def test_load_okf_rules_in_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rules_dir = repo_root / ".adk" / "knowledge" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            (rules_dir / "01_security.md").write_text("Never commit private keys.", encoding="utf-8")
            (rules_dir / "02_testing.md").write_text("Enforce TDD Red check.", encoding="utf-8")

            loaded_rules = _load_okf_rules(repo_root, repo_root)
            self.assertIn("01_security", loaded_rules)
            self.assertIn("Never commit private keys.", loaded_rules)
            self.assertIn("02_testing", loaded_rules)
            self.assertIn("Enforce TDD Red check.", loaded_rules)


if __name__ == "__main__":
    unittest.main()
