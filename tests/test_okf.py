from __future__ import annotations

import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from adk_dev_flow.gitlab_client import GitLabClient
from adk_dev_flow.mcp_server import FlowService
from adk_dev_flow.models import AtomicTask, ReviewVerdict, SpecContract
from adk_dev_flow.okf import TYPE_REGISTRY, split_frontmatter, validate_repo
from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.workflow import _load_okf_rules

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_cmd(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _errors(report: dict) -> list[str]:
    return [i["message"] for i in report["errors"]]


# --- Drift gates: facts that documentation states and code owns -------------
# A markdown file without an OKF `type` is skipped silently by validate_repo.
# These are the only tracked files allowed to stay untyped.
LOADER_CONTRACT_DOCS = {  # frontmatter belongs to the skill loader (name/description)
    "src/adk_dev_flow/templates/SKILL.md",
    "plugin/adk-dev-flow/skills/deterministic-dev/SKILL.md",
}
GENERATED_OR_EXPORTED_DOCS = {  # produced by a generator / export, never hand-edited
    "plugin/adk-dev-flow/README.md",  # plugin_builder.build_plugin_bundle
    "plugin/adk-dev-flow/rules/AGENTS.md",  # frontmatter stripped by strip_yaml_frontmatter
    "mod/pixel-forge-screen/README.md",  # Mod Center export
}
PLUGIN_GENERATED_DOCS = (
    "README.md",
    "skills/deterministic-dev/SKILL.md",
    "rules/AGENTS.md",
)
ENV_READ_RE = re.compile(r"""(?:environ\.get|getenv|_env)\(\s*["']([A-Z][A-Z0-9_]+)["']""")
TOOL_COUNT_RE = re.compile(r"(\d+)(?:\s*个)?\s*(?:FastMCP\s+)?(?:原子)?工具")


def _tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _registered_tools() -> dict[str, str]:
    """flow_* MCP tools as the server registers them: name -> 异步/同步."""
    tree = ast.parse((REPO_ROOT / "src" / "adk_dev_flow" / "mcp_server.py").read_text(encoding="utf-8"))
    tools: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(ast.unparse(d).startswith(("mcp.tool", "mcp.async_tool")) for d in node.decorator_list):
            tools[node.name] = "异步" if isinstance(node, ast.AsyncFunctionDef) else "同步"
    return tools


def _env_names_read_by_code() -> dict[str, str]:
    """Environment variable names read under src/ -> first source location."""
    found: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "src" / "adk_dev_flow").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in ENV_READ_RE.finditer(text):
            found.setdefault(match.group(1), f"{path.relative_to(REPO_ROOT).as_posix()}:{text.count(chr(10), 0, match.start()) + 1}")
    return found


def _readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _readme_env_table() -> dict[str, str]:
    """Variable names listed in the README env table -> the row text."""
    rows: dict[str, str] = {}
    for line in _readme().splitlines():
        match = re.match(r"^\| ((?:`[A-Z][A-Z0-9_]*`(?: / )?)+) \|", line)
        if match:
            for name in re.findall(r"[A-Z][A-Z0-9_]+", match.group(1)):
                rows[name] = line
    return rows


class TestOkfContract(unittest.TestCase):
    """OKF 1.0 executable contract: validator, corruption injection, queries."""

    def test_repo_corpus_valid(self) -> None:
        report = validate_repo(REPO_ROOT)
        self.assertTrue(report["valid"], f"corpus errors: {_errors(report)}")
        self.assertEqual(report["warnings"], [], f"corpus warnings: {report['warnings']}")
        counts = report["type_counts"]
        for expected in (
            "DevSpec",
            "EngineeringRule",
            "KnowledgeIndex",
            "ArchitectureDoc",
            "Assessment",
            "ADR",
            "OpsGuide",
        ):
            self.assertGreaterEqual(counts.get(expected, 0), 1, f"{expected} missing from corpus")

    def test_untyped_markdown_is_an_explicit_allowlist(self) -> None:
        """Gate G1: a new or de-typed document cannot fall out of OKF scope unnoticed."""
        untyped: set[str] = set()
        loader_contract: set[str] = set()
        for rel in _tracked_markdown():
            meta, _body, err = split_frontmatter((REPO_ROOT / rel).read_text(encoding="utf-8"))
            self.assertIsNone(err, f"broken frontmatter: {rel}: {err}")
            if meta and meta.get("type"):
                continue
            if meta and "name" in meta:
                loader_contract.add(rel)
            else:
                untyped.add(rel)
        self.assertEqual(loader_contract, LOADER_CONTRACT_DOCS, "loader-contract set changed")
        self.assertEqual(
            untyped,
            GENERATED_OR_EXPORTED_DOCS,
            "untyped markdown set changed: give the file an OKF type or add it to the right set",
        )

    def test_tool_table_and_counts_match_registered_tools(self) -> None:
        """Gate G2: README tool table + every stated tool count follow mcp_server.py."""
        tools = _registered_tools()
        readme = _readme()
        table = dict(re.findall(r"^\| `(flow_[a-z_]+)` \| (异步|同步) \|", readme, re.M))
        self.assertEqual(table, tools, "README tool table differs from registered tools (name or 异步/同步)")

        counts_in_readme = TOOL_COUNT_RE.findall(readme)
        self.assertGreaterEqual(len(counts_in_readme), 2, "tool-count statements not found in README")
        builder = (REPO_ROOT / "src" / "adk_dev_flow" / "plugin_builder.py").read_text(encoding="utf-8")
        counts_in_builder = TOOL_COUNT_RE.findall(builder)
        self.assertGreaterEqual(len(counts_in_builder), 3, "tool-count statements not found in plugin_builder.py")
        for where, counts in (("README.md", counts_in_readme), ("plugin_builder.py", counts_in_builder)):
            self.assertEqual(set(counts), {str(len(tools))}, f"{where}: stated tool count(s) {sorted(set(counts))} != {len(tools)}")

    def test_env_table_covers_every_variable_read_by_code(self) -> None:
        """Gate G3: the README env table is a complete inventory of src/ env reads."""
        rows = _readme_env_table()
        read = _env_names_read_by_code()
        missing = {name: loc for name, loc in read.items() if name not in rows}
        self.assertEqual(missing, {}, f"env vars read by code but absent from README table: {missing}")

    def test_generated_plugin_docs_match_fresh_build(self) -> None:
        """Gate G4: plugin/ markdown equals what plugin_builder emits now (no hand edits, no stale build)."""
        from adk_dev_flow.plugin_builder import build_plugin_bundle

        with tempfile.TemporaryDirectory() as tmp:
            build_plugin_bundle(REPO_ROOT, Path(tmp))
            for rel in PLUGIN_GENERATED_DOCS:
                committed = (REPO_ROOT / "plugin" / "adk-dev-flow" / rel).read_bytes()
                fresh = (Path(tmp) / rel).read_bytes()
                self.assertEqual(committed, fresh, f"plugin/adk-dev-flow/{rel} is not a fresh build output")

    def test_spec_template_is_lockable(self) -> None:
        """Gate G5: the DevSpec template copied by `adk-dev-flow-init` parses as a lockable spec."""
        spec = SpecContract.from_okf_markdown(REPO_ROOT / "src" / "adk_dev_flow" / "templates" / "spec_template.md")
        self.assertEqual(spec.issue_iid, 1)
        self.assertFalse(spec.spike)
        self.assertTrue(spec.allowed_paths)

    def test_registry_covers_all_known_types(self) -> None:
        expected = {
            "DevSpec", "ReviewVerdict", "EngineeringRule", "KnowledgeIndex",
            "AdoptedSpec", "ADR", "ArchitectureDoc", "Assessment",
            "Note", "OpsGuide", "RequirementMatrix", "TaskPlan",
        }
        self.assertEqual(set(TYPE_REGISTRY.keys()), expected)

    def test_broken_supersedes_symmetry_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "docs/adr/0009-x.md").write_text(
                "---\ntype: ADR\nadr: 9\ntitle: 'T9'\nstatus: accepted\ndate: '2026-11-25'\n"
                "supersedes_partial: [3]\n---\n\n# ADR 9\n",
                encoding="utf-8",
            )
            broken = (
                "---\ntype: ADR\nadr: 3\ntitle: 'T3'\nstatus: accepted\ndate: '2026-11-20'\n"
                "superseded_by_partial: []\n---\n\n# ADR 3\n"
            )
            fixed = broken.replace("superseded_by_partial: []", "superseded_by_partial: [9]")
            target = repo / "docs/adr/0003-y.md"
            target.write_text(broken, encoding="utf-8")
            self.assertIn("对称性破坏", " ".join(_errors(validate_repo(repo))))
            target.write_text(fixed, encoding="utf-8")
            self.assertEqual(_errors(validate_repo(repo)), [])

    def test_reviewverdict_internal_contradiction_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".adk" / "audit").mkdir(parents=True)
            (repo / ".adk/audit/review_bad.md").write_text(
                "---\ntype: ReviewVerdict\nrun_id: r1\napproved: true\n"
                "verified: process:acceptance_gate_failed\nstatus: rejected\n"
                "timestamp: '2026-11-25T00:00:00+00:00'\n---\n\n# verdict\n",
                encoding="utf-8",
            )
            self.assertIn("内部矛盾", " ".join(_errors(validate_repo(repo))))

    def test_fabricated_run_id_is_error_when_db_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            StateStore(repo)  # creates .adk/workflow.db
            (repo / ".adk" / "specs").mkdir(parents=True)
            (repo / ".adk/specs/issue-76.md").write_text(
                "---\ntype: AdoptedSpec\ntitle: 'X'\nstatus: finalized\nissue_iid: 76\n"
                "branch: '76-x'\nhead_sha: 'abc123'\ncommit_range: ''\nevidence_type: post_hoc\n"
                "run_id: deadbeef0000\nadopted_at: '2026-11-25T00:00:00+00:00'\n---\n\n# X\n",
                encoding="utf-8",
            )
            self.assertIn("不存在于", " ".join(_errors(validate_repo(repo))))

    def test_unknown_type_and_extra_keys_are_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "docs").mkdir()
            (repo / "docs/w.md").write_text(
                "---\ntype: FutureThing\ntitle: 'W'\nstatus: final\n---\n\n# W\n", encoding="utf-8"
            )
            (repo / "docs/ok.md").write_text(
                "---\ntype: Note\ntitle: 'N'\nstatus: final\ndate: '2026-11-25'\ncustom_extra: 1\n---\n\n# N\n",
                encoding="utf-8",
            )
            report = validate_repo(repo)
            self.assertTrue(report["valid"])  # warnings do not fail
            joined = " ".join(i["message"] for i in report["warnings"])
            self.assertIn("未知 OKF 类型", joined)
            self.assertIn("custom_extra", joined)

    def test_status_out_of_domain_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "docs/adr/0001-a.md").write_text(
                "---\ntype: ADR\nadr: 1\ntitle: 'A'\nstatus: approved-forever\ndate: '2026-11-20'\n---\n\n# A\n",
                encoding="utf-8",
            )
            report = validate_repo(repo)
            self.assertTrue(report["valid"])
            self.assertIn("已知值域", " ".join(i["message"] for i in report["warnings"]))

    def test_loader_contract_frontmatter_is_skipped_as_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            skill = repo / ".codebuddy" / "skills" / "x"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: x\ndescription: loader contract\n---\n\n# X\n", encoding="utf-8"
            )
            report = validate_repo(repo)
            self.assertTrue(report["valid"])
            self.assertTrue(any("加载器契约" in i["message"] for i in report["infos"]))

    @settings(max_examples=30, deadline=None)
    @given(
        title=st.text(min_size=1, max_size=60).filter(lambda s: s.strip() and ":" not in s and "#" not in s),
        issue=st.integers(min_value=1, max_value=9999),
        spike=st.booleans(),
    )
    def test_devspec_meta_survives_yaml_round_trip(self, title: str, issue: int, spike: bool) -> None:
        from adk_dev_flow.okf import DevSpecMeta

        meta = {
            "type": "DevSpec",
            "title": title,
            "status": "active",
            "issue_iid": issue,
            "target_branch": "main",
            "require_tdd_red": True,
            "spike": spike,
            "extra_dependencies": [],
            "allowed_paths": ["src/pkg"],
            "acceptance_test_path": "tests/test_x.py",
            "tasks": [{"id": "T1", "title": "t", "description": "d", "target_files": ["src/pkg/a.py"]}],
        }
        text = f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)}---\n\n# body\n"
        parsed, _body, err = split_frontmatter(text)
        self.assertIsNone(err)
        first = DevSpecMeta.model_validate(parsed)
        again = DevSpecMeta.model_validate(yaml.safe_load(yaml.safe_dump(parsed)))
        self.assertEqual(first.title, again.title)
        self.assertEqual(first.issue_iid, again.issue_iid)
        self.assertEqual(first.spike, again.spike)
        self.assertEqual(first.allowed_paths, again.allowed_paths)

    def test_query_zombie_runs_reports_stale_active_only(self) -> None:
        from adk_dev_flow.okf import query_zombie_runs

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git_cmd(repo, "init", "-q")
            (repo / "README.md").write_text("x", encoding="utf-8")
            _git_cmd(repo, "add", "README.md")
            _git_cmd(repo, "commit", "-q", "-m", "init")
            store = StateStore(repo)
            store.create_run("r66", issue_iid=66, status="MR_READY")
            store.create_run("r67", issue_iid=67, status="ABORTED")
            store.create_run("r68", issue_iid=68, status="SPEC_DRAFTING")
            import sqlite3

            conn = sqlite3.connect(str(repo / ".adk" / "workflow.db"))
            conn.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = 'r68'",
                ("2026-01-01T00:00:00.000000+00:00",),
            )
            conn.commit()
            conn.close()

            res = query_zombie_runs(repo, threshold_hours=24.0)
            ids = [z["run_id"] for z in res["zombies"]]
            self.assertIn("r68", ids)
            self.assertNotIn("r66", ids)
            self.assertNotIn("r67", ids)  # ABORTED is not active

    def test_query_spec_coverage_matches_constructed_facts(self) -> None:
        from adk_dev_flow.okf import query_spec_coverage

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git_cmd(repo, "init", "-q")
            (repo / "README.md").write_text("x", encoding="utf-8")
            _git_cmd(repo, "add", "README.md")
            _git_cmd(repo, "commit", "-q", "-m", "init")
            store = StateStore(repo)
            store.create_run("r66", issue_iid=66, status="MR_READY")
            store.create_run("r67", issue_iid=67, status="ABORTED")
            store.create_run("r68", issue_iid=68, status="SPEC_DRAFTING")
            specs = repo / ".adk" / "specs"
            specs.mkdir(parents=True, exist_ok=True)
            (specs / "issue-66.md").write_text(
                "---\ntype: DevSpec\ntitle: 'S66'\nstatus: active\nissue_iid: 66\n"
                "allowed_paths: ['src']\nacceptance_test_path: 'tests/t.py'\n"
                "tasks: [{id: 'T1', title: 't', description: 'd', target_files: ['src/a.py']}]\n---\n\n# S66\n",
                encoding="utf-8",
            )

            res = query_spec_coverage(repo)
            self.assertEqual(res["issues_with_runs"], 3)
            self.assertEqual(res["issues_with_spec_docs"], 1)
            missing = {item["issue_iid"] for item in res["runs_without_spec"]}
            self.assertEqual(missing, {67, 68})

    def test_cli_validate_and_query(self) -> None:
        from adk_dev_flow.okf import main

        self.assertEqual(main(["validate", str(REPO_ROOT), "--json"]), 0)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["query", "zombie-runs", tmp]), 0)


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

    def test_load_okf_rules_selective_routing(self) -> None:
        """Verify that rules are selectively routed based on allowed_paths, tags, and status."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rules_dir = repo_root / ".adk" / "knowledge" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)

            (rules_dir / "universal.md").write_text(
                "---\n"
                "type: EngineeringRule\n"
                "title: Universal Standards\n"
                "status: active\n"
                "---\n"
                "Always write clean code.\n",
                encoding="utf-8",
            )
            (rules_dir / "db_rules.md").write_text(
                "---\n"
                "type: EngineeringRule\n"
                "title: DB Rules\n"
                "status: active\n"
                "paths:\n"
                "  - 'src/db/**'\n"
                "  - 'src/models/*'\n"
                "tags:\n"
                "  - database\n"
                "---\n"
                "Explicitly close database connections.\n",
                encoding="utf-8",
            )
            (rules_dir / "api_rules.md").write_text(
                "---\n"
                "type: EngineeringRule\n"
                "title: API Rules\n"
                "status: active\n"
                "paths:\n"
                "  - 'src/api/**'\n"
                "---\n"
                "Always validate request payload schema.\n",
                encoding="utf-8",
            )
            (rules_dir / "deprecated_rule.md").write_text(
                "---\n"
                "type: EngineeringRule\n"
                "title: Old Advice\n"
                "status: deprecated\n"
                "---\n"
                "This advice should never be injected.\n",
                encoding="utf-8",
            )

            # 1. Routing for DB change: should match universal + db, but NOT api or deprecated
            db_rules = _load_okf_rules(
                repo_root,
                repo_root,
                allowed_paths=["src/db/engine.py"],
            )
            self.assertIn("Universal Standards", db_rules)
            self.assertIn("Always write clean code.", db_rules)
            self.assertIn("DB Rules", db_rules)
            self.assertIn("Explicitly close database connections.", db_rules)
            self.assertNotIn("API Rules", db_rules)
            self.assertNotIn("Old Advice", db_rules)

            # 2. Routing for API change: should match universal + api, but NOT db or deprecated
            api_rules = _load_okf_rules(
                repo_root,
                repo_root,
                allowed_paths=["src/api/routes.py"],
            )
            self.assertIn("Universal Standards", api_rules)
            self.assertIn("API Rules", api_rules)
            self.assertNotIn("DB Rules", api_rules)
            self.assertNotIn("Old Advice", api_rules)

    def test_atomic_task_target_symbols_okf_roundtrip_and_traceability(self) -> None:
        """P1-2: Verify target_symbols in AtomicTask serializes to OKF and validates alignment."""
        spec = SpecContract(
            issue_iid=99,
            title="feat: add payment gateway",
            summary="Payment gateway integration.",
            target_branch="main",
            allowed_paths=["src/payment"],
            acceptance_test_path="tests/test_payment.py",
            acceptance_test_code="def test_pay(): assert True\n",
            tasks=[
                AtomicTask(
                    id="T1",
                    title="Implement StripeGateway",
                    description="Core gateway logic",
                    target_files=["src/payment/stripe.py"],
                    target_symbols=["StripeGateway.charge", "StripeGateway.refund"],
                ),
            ],
        )

        okf_md = spec.to_okf_markdown()
        self.assertIn("target_symbols:", okf_md)
        self.assertIn("StripeGateway.charge", okf_md)

        # Parse back
        loaded = SpecContract.from_okf_markdown(okf_md)
        self.assertEqual(loaded.tasks[0].target_symbols, ["StripeGateway.charge", "StripeGateway.refund"])

        # Check alignment helper
        from adk_dev_flow.workflow import audit_symbol_alignment

        # Case 1: definitions contain all declared symbols
        definitions = {
            "src/payment/stripe.py": ["StripeGateway.charge", "StripeGateway.refund", "StripeGateway.__init__"]
        }
        missing = audit_symbol_alignment(loaded.tasks, definitions)
        self.assertEqual(missing, [])

        # Case 2: definitions miss one symbol
        partial_defs = {
            "src/payment/stripe.py": ["StripeGateway.charge"]
        }
        missing2 = audit_symbol_alignment(loaded.tasks, partial_defs)
        self.assertEqual(len(missing2), 1)
        self.assertIn("StripeGateway.refund", missing2[0])

    def test_distill_knowledge_from_run_lifecycle(self) -> None:
        """P2: Verify experience distillation creates valid OKF rules that feed back into routing."""
        from adk_dev_flow.workflow import distill_knowledge_from_run

        verdict = ReviewVerdict(
            approved=True,
            confidence=0.95,
            summary="Fixed Redis lock TTL expiration issue after handling connection timeout.",
            blocking_issues=[],
        )

        # 1. Attempt 0 straightforward pass should return None (no noisy rule needed)
        no_rule = distill_knowledge_from_run(
            spec=self.sample_spec,
            run_id="run-pass-fast",
            verdict=verdict,
            attempt=0,
        )
        self.assertIsNone(no_rule)

        # 2. Attempt 2 with repair experience generates an OKF EngineeringRule
        rule_md = distill_knowledge_from_run(
            spec=self.sample_spec,
            run_id="run-fixed-hard",
            verdict=verdict,
            attempt=2,
            last_error_log="ConnectionTimeoutError: redis host unreachable on port 6379",
        )
        self.assertIsNotNone(rule_md)
        self.assertIn("type: EngineeringRule", rule_md)
        self.assertIn("Learned Rule: feat: add distributed lock", rule_md)
        self.assertIn("ConnectionTimeoutError", rule_md)
        self.assertIn("src/lock", rule_md)

        # 3. Verify it is routable by _load_okf_rules
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rules_dir = repo_root / ".adk" / "knowledge" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            (rules_dir / "learned_run1234.md").write_text(rule_md, encoding="utf-8")

            # Match on allowed_paths
            matched = _load_okf_rules(repo_root, repo_root, allowed_paths=["src/lock/client.py"])
            self.assertIn("Learned Rule", matched)
            self.assertIn("ConnectionTimeoutError", matched)

            # Do not match on unrelated paths
            unrelated = _load_okf_rules(repo_root, repo_root, allowed_paths=["src/frontend/app.js"])
            self.assertNotIn("Learned Rule", unrelated)


if __name__ == "__main__":
    unittest.main()
