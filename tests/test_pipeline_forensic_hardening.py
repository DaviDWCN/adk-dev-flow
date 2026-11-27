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

from adk_dev_flow.codebuddy_bridge import CodeBuddyLlm
from adk_dev_flow.forge_client import BaseGitForge
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, SpecContract
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.workflow import build_dev_workflow, execute_workflow_run
from adk_dev_flow.worktree_utils import (
    audit_worktree_changes,
    compute_file_sha256,
    sanitize_worktree_state,
)
from google.adk.models.llm_request import LlmRequest
from google.genai import types


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


class _RecordingOnlineForge(BaseGitForge):
    def __init__(self) -> None:
        self.mr_calls: list[dict] = []
        self.note_calls: list[tuple[int, str]] = []

    @property
    def platform_name(self) -> str:
        return "gitlab"

    @property
    def enabled(self) -> bool:
        return True

    def get_issue(self, issue_iid: int) -> dict:
        return {"iid": issue_iid, "title": "Issue", "description": "Desc"}

    def create_or_update_mr(self, **kwargs) -> dict:
        self.mr_calls.append(kwargs)
        return {"iid": len(self.mr_calls), "web_url": f"https://gitlab.example.com/mr/{len(self.mr_calls)}"}

    def post_mr_note(self, mr_iid: int, body: str) -> None:
        self.note_calls.append((mr_iid, body))

    def mark_mr_ready(self, mr_iid: int) -> dict:
        return {"iid": mr_iid, "ready": True}


class TestPipelineForensicHardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bare = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.bare.mkdir()
        self.repo.mkdir()
        _git(self.bare, "init", "--bare")
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "remote", "add", "origin", str(self.bare))
        (self.repo / "src" / "mypkg").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "mypkg" / "__init__.py").write_text(
            "ORIGIN_TAG = 'MAIN'\ndef solve() -> int:\n    return 0\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "unrelated_dirty.py").write_text(
            "import os  # unused import outside allowed_paths\n",
            encoding="utf-8",
        )
        (self.repo / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "chore: baseline")
        _git(self.repo, "push", "-u", "origin", "main")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_skip_worktree_evasion_detected_by_real_git_audit(self) -> None:
        """Real git update-index --skip-worktree emits uppercase 'S' and must be caught."""
        locked = self.repo / "tests" / "test_lock.py"
        locked.write_text("def test_ok(): assert True\n", encoding="utf-8")
        sha = compute_file_sha256(locked)
        _git(self.repo, "add", "tests/test_lock.py")
        _git(self.repo, "commit", "-m", "lock test")
        lock_commit = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "update-index", "--skip-worktree", ".gitlab-ci.yml")
        (self.repo / ".gitlab-ci.yml").write_text("stages: []\n", encoding="utf-8")

        audit = await audit_worktree_changes(
            self.repo,
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_lock.py",
            locked_test_sha256=sha,
            locked_test_commit_sha=lock_commit,
        )
        self.assertFalse(audit.ok)
        self.assertTrue(
            any(".gitlab-ci.yml" in v and "索引伪装标记" in v for v in audit.violations),
            f"Expected skip-worktree violation for .gitlab-ci.yml, got: {audit.violations}",
        )

    async def test_relative_gitdir_sanitizes_lock_and_detects_exclude_evasion(self) -> None:
        """Worktree .git files with relative gitdir paths must resolve against worktree, not cwd."""
        wt_dir = self.root / "fake_wt"
        rel_gitdir_name = "actual_gitdir"
        actual_gitdir = wt_dir / rel_gitdir_name
        (actual_gitdir / "info").mkdir(parents=True)
        (wt_dir / "tests").mkdir(parents=True)
        (wt_dir / ".git").write_text(f"gitdir: {rel_gitdir_name}\n", encoding="utf-8")

        stale_lock = actual_gitdir / "index.lock"
        stale_lock.write_text("stale", encoding="utf-8")
        await sanitize_worktree_state(wt_dir)
        self.assertFalse(stale_lock.exists(), "Relative gitdir index.lock must be cleaned up")

        # Non-comment line in relative gitdir info/exclude must be caught by audit_worktree_changes
        exclude = actual_gitdir / "info" / "exclude"
        exclude.write_text("evil_hidden.py\n", encoding="utf-8")
        locked = wt_dir / "tests" / "test_x.py"
        locked.write_text("def test_x(): assert True\n", encoding="utf-8")
        sha = compute_file_sha256(locked)
        audit = await audit_worktree_changes(
            wt_dir,
            allowed_paths=["src/"],
            acceptance_test_path="tests/test_x.py",
            locked_test_sha256=sha,
            locked_test_commit_sha="",
        )
        self.assertFalse(audit.ok)
        self.assertTrue(any("info/exclude" in v for v in audit.violations), audit.violations)

    async def test_e2e_shim_worker_worktree_pythonpath_glob_lint_distill_and_push_gate(self) -> None:
        """End-to-end real-git + bare-origin + sqlite3 + CODEBUDDY_BIN shim tests:
        1. Worker subprocess imports worktree/src ahead of main repo PYTHONPATH.
        2. Glob allowed_paths (e.g. 'src/mypkg/*.py') does NOT fall back to 'ruff check .' on unrelated_dirty.py.
        3. Approved repair run pushes to bare origin and distills real round-0 failure from attempt_logs.
        4. AST-vetoed run NEVER pushes to bare origin and does NOT distill a learned rule.
        """
        shim_py = self.root / "shim_worker.py"
        shim_py.write_text(
            """import json, os, pathlib, sys
prompt = sys.stdin.read() if "-p" in sys.argv and sys.argv[-1] == "-p" else sys.argv[-1]
mode = os.environ.get("SHIM_MODE", "repair_then_pass")
# Verify worker sees worktree src ahead of main repo src in PYTHONPATH
sys.path[:] = [p for p in sys.path if p]
import mypkg
wt = pathlib.Path.cwd()
marker_out = wt / ".shim_imported_from.txt"
# Write outside python import check? No, record in memory / stdout summary only!
imported_file = str(pathlib.Path(mypkg.__file__).resolve())
target = wt / "src" / "mypkg" / "__init__.py"
if mode == "ast_veto":
    target.write_text("ORIGIN_TAG = 'WORKTREE'\\ndef solve() -> int:\\n    return eval('42')\\n", encoding="utf-8")
    print(json.dumps({"changed_files": ["src/mypkg/__init__.py"], "summary": f"imported={imported_file}"}))
elif "第 1/" in prompt or "第 2/" in prompt:
    target.write_text("ORIGIN_TAG = 'WORKTREE'\\ndef solve() -> int:\\n    return 42\\n", encoding="utf-8")
    print(json.dumps({"changed_files": ["src/mypkg/__init__.py"], "summary": f"fixed; imported={imported_file}"}))
else:
    target.write_text("ORIGIN_TAG = 'WORKTREE'\\ndef solve() -> int:\\n    return 1\\n", encoding="utf-8")
    print(json.dumps({"changed_files": ["src/mypkg/__init__.py"], "summary": f"stub1; imported={imported_file}"}))
""",
            encoding="utf-8",
        )

        store = StateStore(self.repo)
        forge = _RecordingOnlineForge()
        service = FlowService(repo_root=self.repo, store=store, forge=forge)

        # --- Scenario 1: Repair -> Pass -> Push -> Distill ---
        init1 = await service.flow_init("Implement solve() == 42")
        run1 = init1["run_id"]
        spec1 = SpecContract(
            title="Solve 42",
            summary="Return 42 from mypkg.solve",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code=(
                "from mypkg import solve\n"
                "def test_solve():\n"
                "    assert solve() == 42\n"
            ),
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Implement solve",
                    description="Return 42",
                    target_files=["src/mypkg/__init__.py"],
                )
            ],
        )
        lock1 = service.flow_lock_spec(run1, spec1)
        self.assertEqual(lock1["status"], "SPEC_LOCKED")

        env_shim = {
            "ADK_WORKER_PROVIDER": "codebuddy",
            "CODEBUDDY_BIN": sys.executable,
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim_py)]),
            "CODEBUDDY_USE_STDIN": "1",
            "ADK_TEST_CMD": f'"{sys.executable}" -m pytest -q {{test_path}}',
            "PYTHONPATH": str(self.repo / "src"),  # deliberately point to MAIN repo!
            "SHIM_MODE": "repair_then_pass",
            "ADK_ENABLE_CRITIC": "0",
        }
        with patch.dict(os.environ, env_shim, clear=False):
            await execute_workflow_run(self.repo, store, forge, run1)

        # Verify SQLite DB state directly
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        row1 = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run1,)).fetchone())
        logs1 = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attempt_logs WHERE run_id = ? ORDER BY attempt ASC", (run1,)
            ).fetchall()
        ]
        conn.close()

        self.assertEqual(row1["status"], "MR_READY", row1["last_error_log"])
        self.assertEqual(len(logs1), 2)
        self.assertIn("assert 1 == 42", logs1[0]["combined_err"])
        # Worker must have imported from worktree/src, NOT main repo/src!
        wt1 = Path(row1["worktree_path"]).resolve()
        self.assertIn(str((wt1 / "src" / "mypkg" / "__init__.py").resolve()), logs1[0]["worker_summary"])

        # Physical bare origin check: branch pushed
        ls_remote_1 = _git(self.bare, "ls-remote", "--heads", str(self.bare), row1["branch"])
        self.assertTrue(ls_remote_1, f"Approved branch {row1['branch']} must exist on bare origin")

        # Distilled rule must capture the REAL round-0 failure, not '[Verification] ✅'
        learned1 = wt1 / ".adk" / "knowledge" / "rules" / f"learned_{run1[:8]}.md"
        self.assertTrue(learned1.is_file(), "Approved repair run must distill OKF rule")
        learned_txt = learned1.read_text(encoding="utf-8")
        self.assertIn("assert 1 == 42", learned_txt)
        self.assertNotIn("[Verification]", learned_txt)
        # .adk/cache/cpg_cache.db created by _build_ast_context must NOT be committed to branch
        self.assertEqual(_git(wt1, "ls-files", ".adk/cache"), "")

        # --- Scenario 2: AST Veto -> Escalated -> NEVER pushed & NO distilled rule ---
        init2 = await service.flow_init("Unsafe eval solve")
        run2 = init2["run_id"]
        spec2 = SpecContract(
            title="Evil eval",
            summary="Should be vetoed by AST gate",
            allowed_paths=["src/mypkg/"],
            acceptance_test_path="tests/test_solve.py",
            acceptance_test_code=(
                "from mypkg import solve\n"
                "def test_solve():\n"
                "    assert solve() == 42\n"
            ),
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Implement solve",
                    description="Return 42",
                    target_files=["src/mypkg/__init__.py"],
                )
            ],
        )
        service.flow_lock_spec(run2, spec2)
        mr_count_before = len(forge.mr_calls)
        env_veto = {**env_shim, "SHIM_MODE": "ast_veto"}
        with patch.dict(os.environ, env_veto, clear=False):
            await execute_workflow_run(self.repo, store, forge, run2)

        row2 = store.get_run(run2)
        self.assertEqual(row2["status"], "ESCALATED")
        self.assertIn("eval", row2["last_error_log"])
        self.assertEqual(len(forge.mr_calls), mr_count_before, "Vetoed run must not create MR")

        ls_remote_2 = _git(self.bare, "ls-remote", "--heads", str(self.bare), row2["branch"])
        self.assertEqual(ls_remote_2, "", "Vetoed run branch must NEVER be pushed to bare origin")

        wt2 = Path(row2["worktree_path"]).resolve()
        learned2 = wt2 / ".adk" / "knowledge" / "rules" / f"learned_{run2[:8]}.md"
        self.assertFalse(learned2.exists(), "Rejected/vetoed run must NOT distill a learned rule")

    async def test_codebuddy_llm_strips_port_and_uses_port_0(self) -> None:
        """CodeBuddyLlm must strip parent PORT/CODEBUDDY_PORT and pass --port 0."""
        shim_llm = self.root / "shim_llm.py"
        shim_llm.write_text(
            """import json, os, sys
print(json.dumps({
    "approved": True,
    "blocking_issues": [],
    "summary": json.dumps({"argv": sys.argv[1:], "has_port": "PORT" in os.environ, "has_cb_port": "CODEBUDDY_PORT" in os.environ}),
}))
""",
            encoding="utf-8",
        )
        llm = CodeBuddyLlm(model="codebuddy-cli", cli_bin=sys.executable)
        req = LlmRequest(
            model="codebuddy-cli",
            contents=[types.Content(role="user", parts=[types.Part(text="review diff")])],
        )
        env_patch = {
            "PORT": "52445",
            "CODEBUDDY_PORT": "52445",
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim_llm)]),
        }
        chunks: list[str] = []
        with patch.dict(os.environ, env_patch, clear=False):
            async for resp in llm.generate_content_async(req):
                chunks.append(resp.content.parts[0].text)

        parsed = json.loads(chunks[0])
        meta = json.loads(parsed["summary"])
        self.assertFalse(meta["has_port"])
        self.assertFalse(meta["has_cb_port"])
        argv = meta["argv"]
        self.assertIn("--port", argv)
        self.assertEqual(argv[argv.index("--port") + 1], "0")

    async def test_verify_skips_lint_instead_of_scanning_repo_when_allowed_paths_deleted(self) -> None:
        """When allowed_paths targets are deleted (lint_targets=[]), ruff must NOT fall back to '.'."""
        (self.repo / "src" / "to_delete.py").write_text("X = 1\n", encoding="utf-8")
        _git(self.repo, "add", "src/to_delete.py")
        _git(self.repo, "commit", "-m", "add deletable file")

        shim_del = self.root / "shim_del.py"
        shim_del.write_text(
            """import json, pathlib
(pathlib.Path.cwd() / "src" / "to_delete.py").unlink(missing_ok=True)
print(json.dumps({"changed_files": ["src/to_delete.py"], "summary": "deleted"}))
""",
            encoding="utf-8",
        )

        store = StateStore(self.repo)
        forge = _RecordingOnlineForge()
        service = FlowService(repo_root=self.repo, store=store, forge=forge)
        run_id = (await service.flow_init("Delete deprecated module"))["run_id"]
        spec = SpecContract(
            title="Delete to_delete.py",
            summary="Remove obsolete file",
            allowed_paths=["src/to_delete.py"],
            acceptance_test_path="tests/test_del.py",
            acceptance_test_code="from pathlib import Path\ndef test_deleted():\n    assert not Path('src/to_delete.py').exists()\n",
            require_tdd_red=True,
            require_mutation_gate=False,
            tasks=[AtomicTask(id="T1", title="Delete", description="Delete", target_files=["src/to_delete.py"])],
        )
        service.flow_lock_spec(run_id, spec)

        env_del = {
            "ADK_WORKER_PROVIDER": "codebuddy",
            "CODEBUDDY_BIN": sys.executable,
            "CODEBUDDY_BIN_ARGS": json.dumps([str(shim_del)]),
            "CODEBUDDY_USE_STDIN": "1",
            "ADK_TEST_CMD": f'"{sys.executable}" -m pytest -q {{test_path}}',
            "ADK_ENABLE_CRITIC": "0",
        }
        with patch.dict(os.environ, env_del, clear=False):
            await execute_workflow_run(self.repo, store, forge, run_id)

        row = store.get_run(run_id)
        self.assertEqual(row["status"], "MR_READY", f"Must not fail on unrelated_dirty.py: {row['last_error_log']}")
