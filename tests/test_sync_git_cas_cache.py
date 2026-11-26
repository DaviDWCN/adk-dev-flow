"""Acceptance tests for GitHub issue #4 (CodeGraph 默认走 sync_git 与 CAS 并加缓存版本).

三条根因各对应红测 + 一条回归守卫：
1. 缓存投毒：脏工作区下 sync_git 入库内容必须等于提交内容（修复前红）。
2. 版本不一致：旧版本库重开时整库重建（修复前红）。
3. 未命中路径不读工作区：read_text 对目标文件抛错，sync_git 仍取到提交内容（修复前红）。
4. 默认 store + 非 git 仓库回退 index_repo（store 默认断言基线为红）。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adk_dev_flow.ast_graph import CodeGraph
from adk_dev_flow.cpg import CpgStore, compute_content_hash

COMMITTED = "def committed(x):\n    return x\n"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


class _GitRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init", "-q", "--initial-branch=main")
        (self.repo / "svc.py").write_text(COMMITTED, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.blob_oid = _git(self.repo, "rev-parse", "HEAD:svc.py")
        self.db = self.repo / ".adk" / "cache" / "cpg.db"
        self._stores: list[CpgStore] = []

    def _store(self) -> CpgStore:
        store = CpgStore(self.db)
        self._stores.append(store)
        return store

    def tearDown(self) -> None:
        for store in self._stores:
            if store is None:
                continue
            close = getattr(store, "close", None)  # close() 是 T2 产出，基线不存在
            if callable(close):
                close()
            elif getattr(store, "_conn", None) is not None:
                store._conn.close()
        self.tmp.cleanup()


class TestSyncGitNoCachePoisoning(_GitRepo):
    def test_dirty_worktree_caches_committed_content(self) -> None:
        (self.repo / "svc.py").write_text("def poisoned(x):\n    return x\n", encoding="utf-8")
        cg = CodeGraph(self.repo, store=self._store())
        summary = cg.sync_git()
        self.assertEqual(summary["parsed_files"], 1)
        self.assertEqual(
            cg.file_cpgs["svc.py"].content_hash,
            compute_content_hash(COMMITTED),
            "脏工作区下缓存入库内容必须等于提交内容（blob OID 语义）",
        )

    def test_cache_miss_never_reads_working_tree(self) -> None:
        (self.repo / "svc.py").write_text("DIRTY = 1\n", encoding="utf-8")
        real_read_text = Path.read_text
        calls = {"count": 0}

        def counting_read(self_path: Path, *args, **kwargs):
            if self_path.name == "svc.py":
                calls["count"] += 1
            return real_read_text(self_path, *args, **kwargs)

        cg = CodeGraph(self.repo, store=self._store())
        with patch.object(Path, "read_text", counting_read):
            summary = cg.sync_git()
        self.assertEqual(summary["parsed_files"], 1)
        self.assertEqual(calls["count"], 0, "sync_git 缓存未命中路径不得读工作区文件")
        self.assertEqual(
            cg.file_cpgs["svc.py"].content_hash,
            compute_content_hash(COMMITTED),
        )


class TestStoreSchemaVersion(_GitRepo):
    def test_version_mismatch_triggers_full_rebuild(self) -> None:
        db = self.db
        self._store()  # 触发建库
        # 伪造旧版本库：改写 meta 版本 + 塞入一条缓存行
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO cpg_store_meta (key, value) VALUES ('schema_version', '0')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO cpg_blob_cache (blob_oid, file_path, content_hash, cpg_json_zlib)"
                " VALUES ('stale-oid', 'svc.py', 'stale-hash', X'00')"
            )
            conn.commit()
        finally:
            conn.close()

        reopened = self._store()
        self.assertIsNone(
            reopened.get_blob_cpg("stale-oid"),
            "版本不一致必须整库重建，旧行不得残留",
        )
        conn = sqlite3.connect(db)
        try:
            version = conn.execute(
                "SELECT value FROM cpg_store_meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(version[0], str(CpgStore.SCHEMA_VERSION))

    def test_same_version_keeps_data(self) -> None:
        db = self.db
        store = self._store()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT OR REPLACE INTO cpg_blob_cache (blob_oid, file_path, content_hash, cpg_json_zlib)"
            " VALUES ('keep-oid', 'svc.py', 'hash', X'00')"
        )
        conn.commit()
        conn.close()
        self._store()  # 重开同版本库
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT content_hash FROM cpg_blob_cache WHERE blob_oid='keep-oid'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "版本一致时不得整库重建")


class TestDefaultStoreAndFallback(_GitRepo):
    def test_codegraph_defaults_to_cas_store(self) -> None:
        cg = CodeGraph(self.repo)
        self._stores.append(cg.store)
        self.assertIsNotNone(cg.store, "CodeGraph 默认必须启用 CAS 存储")
        self.assertTrue(str(cg.store.db_path).replace("\\", "/").endswith(".adk/cache/cpg_cache.db"))

    def test_non_git_repo_falls_back_to_index_repo(self) -> None:
        plain = self.repo / "plain"
        plain.mkdir()
        (plain / "svc.py").write_text("def only(x):\n    return x\n", encoding="utf-8")
        cg = CodeGraph(plain)
        self._stores.append(cg.store)
        report = cg.analyze_impact(["svc.py"])
        self.assertIn("svc.py", cg.file_index)
        self.assertIsNotNone(report)


if __name__ == "__main__":
    unittest.main()