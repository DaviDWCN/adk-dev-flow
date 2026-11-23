import subprocess
import tempfile
import unittest
from pathlib import Path

from adk_dev_flow.ast_graph import (
    CfgEdge,
    CfgNode,
    CodeGraph,
    CpgBuilder,
    CpgEdge,
    CpgStore,
    DfgEdge,
    FileAstInfo,
    FileCpg,
    SymbolDef,
    SymbolKind,
    audit_ast_diff,
    compute_content_hash,
    compute_git_blob_oid,
)


class TestCpgCasCache(unittest.TestCase):
    def test_blob_oid_matches_git(self):
        """Test compute_git_blob_oid produces exact match with git hash-object."""
        sample_texts = [
            "print('hello world')\n",
            "def foo(x: int) -> int:\n    return x * 42\n",
            "",
            "line1\r\nline2\nline3\n",
        ]
        for text in sample_texts:
            calculated_oid = compute_git_blob_oid(text)
            # Use git hash-object via stdin
            res = subprocess.run(
                ["git", "hash-object", "--stdin"],
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
            )
            git_oid = res.stdout.decode("ascii").strip()
            self.assertEqual(calculated_oid, git_oid)

    def test_cpg_builder_cfg_and_dfg(self):
        """Test intra-procedural CFG and DFG construction."""
        import ast

        code = (
            "def process(x):\n"
            "    y = x + 1\n"
            "    if y > 10:\n"
            "        z = y * 2\n"
            "    else:\n"
            "        z = y - 2\n"
            "    return z\n"
        )
        tree = ast.parse(code)
        builder = CpgBuilder(code, file_path="test.py")
        cfg_nodes, cfg_edges, dfg_edges = builder.build(tree)

        self.assertTrue(any(n.node_type == "entry" for n in cfg_nodes))
        self.assertTrue(any(n.node_type == "exit" for n in cfg_nodes))
        self.assertTrue(any(n.node_type == "branch_cond" for n in cfg_nodes))

        # Check edge kinds
        edge_kinds = {e.kind for e in cfg_edges}
        self.assertIn("branch_true", edge_kinds)
        self.assertIn("branch_false", edge_kinds)

        # Check DFG def-use flows
        dfg_sources = {d.source_var for d in dfg_edges}
        dfg_targets = {d.target_var for d in dfg_edges}
        self.assertIn("x", dfg_sources)
        self.assertIn("y", dfg_sources)
        self.assertIn("z", dfg_targets)
        self.assertIn("<return>", dfg_targets)

    def test_cpg_store_cas_caching(self):
        """Test SQLite CAS store puts, gets, and round-trips compressed CPG data."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "code_graph.sqlite"
            with CpgStore(db_path) as store:
                code = "def add(a, b):\n    return a + b\n"
                blob_oid = compute_git_blob_oid(code)
                content_hash = compute_content_hash(code)
                ast_info = FileAstInfo(file_path="math.py", imports=[], definitions={})
                cpg = FileCpg(
                    file_path="math.py",
                    blob_oid=blob_oid,
                    content_hash=content_hash,
                    ast_info=ast_info,
                    cfg_nodes=[CfgNode(id=1, label="entry:add", line=1, node_type="entry")],
                    cfg_edges=[CfgEdge(source=1, target=2, kind="next")],
                    dfg_edges=[DfgEdge(source_var="a", target_var="<return>", source_line=1, target_line=2, kind="def_use")],
                )

                store.put_blob_cpg(blob_oid, "math.py", cpg)
                retrieved = store.get_blob_cpg(blob_oid, ast_info_cls=FileAstInfo)
                self.assertIsNotNone(retrieved)
                self.assertEqual(retrieved.file_path, "math.py")
                self.assertEqual(retrieved.blob_oid, blob_oid)
                self.assertEqual(len(retrieved.cfg_nodes), 1)
                self.assertEqual(len(retrieved.cfg_edges), 1)
                self.assertEqual(len(retrieved.dfg_edges), 1)
                self.assertEqual(retrieved.dfg_edges[0].source_var, "a")

                # Test commit snapshot
                store.put_commit_snapshot("commit123", {"math.py": blob_oid})
                snap = store.get_commit_snapshot("commit123")
                self.assertEqual(snap, {"math.py": blob_oid})

    def test_code_graph_git_incremental_sync(self):
        """Test Git incremental sync using CAS cache."""
        with tempfile.TemporaryDirectory() as td:
            repo_path = Path(td).resolve()
            # Initialize git repo
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestRunner"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@adk.local"], cwd=str(repo_path), check=True, capture_output=True)

            file_a = repo_path / "mod_a.py"
            file_b = repo_path / "mod_b.py"
            file_a.write_text("def func_a():\n    return 10\n", encoding="utf-8")
            file_b.write_text("from mod_a import func_a\ndef func_b():\n    return func_a() * 2\n", encoding="utf-8")

            subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True, capture_output=True)

            store = CpgStore(repo_path / ".adk" / "cache" / "code_graph.sqlite")
            cg = CodeGraph(repo_path, store=store)

            # First sync: both files parsed and cached in CAS
            stats1 = cg.sync_git()
            self.assertEqual(stats1["parsed_files"], 2)
            self.assertEqual(stats1["cached_hits"], 0)
            self.assertIn("mod_a.py", cg.file_index)
            self.assertIn("mod_b.py", cg.file_index)
            self.assertIn("mod_b.py", cg.reverse_file_deps.get("mod_a.py", set()))

            # Second sync without changes: 100% cache hit
            stats2 = cg.sync_git()
            self.assertEqual(stats2["parsed_files"], 0)
            self.assertEqual(stats2["cached_hits"], 2)

            # Modify only mod_b.py
            file_b.write_text("from mod_a import func_a\ndef func_b():\n    return func_a() * 3\n", encoding="utf-8")
            subprocess.run(["git", "add", "mod_b.py"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "update b"], cwd=str(repo_path), check=True, capture_output=True)

            # Third sync: mod_a hits cache, mod_b parsed
            stats3 = cg.sync_git()
            self.assertEqual(stats3["parsed_files"], 1)
            self.assertEqual(stats3["cached_hits"], 1)

            store.close()

    def test_taint_data_flow_reachability(self):
        """Test AST diff audit detecting dangerous calls and DFG data taint flow."""
        bad_code = (
            "def handle_request(req_data):\n"
            "    user_payload = req_data['cmd']\n"
            "    eval(user_payload)\n"
        )
        report = audit_ast_diff("", bad_code, file_path="handler.py")
        self.assertTrue(report.has_issues)
        self.assertTrue(any("eval" in issue for issue in report.dangerous_nodes))


if __name__ == "__main__":
    unittest.main()
