---
type: DevSpec
title: 'fix(cpg): CodeGraph 默认走 sync_git 与 CAS 并加缓存版本'
status: active
issue_iid: 4
target_branch: main
require_tdd_red: true
extra_dependencies: []
allowed_paths:
- src/adk_dev_flow/ast_graph.py
- src/adk_dev_flow/cpg.py
- tests/test_sync_git_cas_cache.py
- README.md
- docs/cpg-cas-architecture.md
acceptance_test_path: tests/test_sync_git_cas_cache.py
tasks:
- id: T1
  title: sync_git 缓存未命中一律走 git cat-file，不读工作区
  description: sync_git 的未命中分支（src/adk_dev_flow/ast_graph.py:676-686）当前先 full_path.read_text
    读工作区、文件缺失才回退 cat-file——脏工作区内容会以干净 blob OID 入库造成缓存投毒。改为未命中一律 subprocess 调 git cat-file
    -p <blob_oid> 取提交内容，删除工作区读取；cat-file 失败按现行为降级 code=""（该文件解析失败不投毒）。
  target_files:
  - src/adk_dev_flow/ast_graph.py
  target_symbols: []
- id: T2
  title: CpgStore 增加 schema 版本，不一致时整库重建
  description: cpg.py 的 CpgStore._init_db（src/adk_dev_flow/cpg.py:388）无 schema 版本。新增常量
    SCHEMA_VERSION 与元数据表 cpg_store_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)，_init_db
    写入当前版本；发现已存在库的版本 != SCHEMA_VERSION 时 DROP 三张缓存表与 meta 表并按当前 schema 重建（用户已拍板整库重建，不做行级迁移）。版本相同则保留数据。同时新增公开
    close() 方法（关闭 sqlite 连接并置 _conn=None，可重复调用），供测试与环境释放使用。
  target_files:
  - src/adk_dev_flow/cpg.py
  target_symbols: []
- id: T3
  title: CodeGraph 默认启用 CAS 并让 analyze_impact 首访走 sync_git
  description: CodeGraph.__init__（ast_graph.py:372-378）store 默认 None，analyze_impact（ast_graph.py:743-746）懒加载走
    index_repo 全量解析，CAS 与 sync_git 生产零调用。改为：store=None 时默认创建 CpgStore(repo_root/.adk/cache/cpg_cache.db)（目录自动创建）；analyze_impact
    在 self.file_index 为空且 self.store 不为 None 时先调 self.sync_git()——sync_git 内部对非 git
    仓库/git 失败已有 index_repo 回退，保持该回退完好。
  target_files:
  - src/adk_dev_flow/ast_graph.py
  target_symbols: []
- id: T4
  title: 同步文档实测结论
  description: 更新 README.md 的 CPG 条目与 docs/cpg-cas-architecture.md 第 4、6 节：默认 CAS、cat-file
    语义、schema 版本与整库重建行为；记录 index_repo 与 sync_git（暖缓存）的实测耗时，只写实测结论，不设性能阈值。
  target_files:
  - README.md
  - docs/cpg-cas-architecture.md
  target_symbols: []
---

# fix(cpg): CodeGraph 默认走 sync_git 与 CAS 并加缓存版本

## Summary
`CodeGraph` 默认不带 CAS 存储（`store=None`），`analyze_impact` 懒加载时走 `index_repo` 全量解析，CAS 与 `sync_git` 在生产路径上零调用方。`sync_git` 存在缓存投毒缺陷：以 HEAD 的 blob OID 作缓存键，未命中时优先读工作区文件，脏内容以干净 OID 入库。`CpgStore` 没有 schema 版本，缓存格式变更后旧库无法识别。

修复原则（用户已确认）：
- 缓存版本化选**整库重建**，不做行级迁移。
- 未命中一律 `git cat-file -p <oid>`，绝不读工作区。
- 非 git 仓库 / git 不可用时仍回退 `index_repo`（保持现有回退语义）。

## Tasks Breakdown

### Task T1: sync_git 缓存未命中一律走 git cat-file，不读工作区
sync_git 的未命中分支（src/adk_dev_flow/ast_graph.py:676-686）当前先 full_path.read_text 读工作区、文件缺失才回退 cat-file——脏工作区内容会以干净 blob OID 入库造成缓存投毒。改为未命中一律 subprocess 调 git cat-file -p <blob_oid> 取提交内容，删除工作区读取；cat-file 失败按现行为降级 code=""（该文件解析失败不投毒）。
- **Target Files**: src/adk_dev_flow/ast_graph.py

### Task T2: CpgStore 增加 schema 版本，不一致时整库重建
cpg.py 的 CpgStore._init_db（src/adk_dev_flow/cpg.py:388）无 schema 版本。新增常量 SCHEMA_VERSION 与元数据表 cpg_store_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)，_init_db 写入当前版本；发现已存在库的版本 != SCHEMA_VERSION 时 DROP 三张缓存表与 meta 表并按当前 schema 重建（用户已拍板整库重建，不做行级迁移）。版本相同则保留数据。同时新增公开 close() 方法（关闭 sqlite 连接并置 _conn=None，可重复调用），供测试与环境释放使用。
- **Target Files**: src/adk_dev_flow/cpg.py

### Task T3: CodeGraph 默认启用 CAS 并让 analyze_impact 首访走 sync_git
CodeGraph.__init__（ast_graph.py:372-378）store 默认 None，analyze_impact（ast_graph.py:743-746）懒加载走 index_repo 全量解析，CAS 与 sync_git 生产零调用。改为：store=None 时默认创建 CpgStore(repo_root/.adk/cache/cpg_cache.db)（目录自动创建）；analyze_impact 在 self.file_index 为空且 self.store 不为 None 时先调 self.sync_git()——sync_git 内部对非 git 仓库/git 失败已有 index_repo 回退，保持该回退完好。
- **Target Files**: src/adk_dev_flow/ast_graph.py

### Task T4: 同步文档实测结论
更新 README.md 的 CPG 条目与 docs/cpg-cas-architecture.md 第 4、6 节：默认 CAS、cat-file 语义、schema 版本与整库重建行为；记录 index_repo 与 sync_git（暖缓存）的实测耗时，只写实测结论，不设性能阈值。
- **Target Files**: README.md, docs/cpg-cas-architecture.md

## Locked Acceptance Test (`tests/test_sync_git_cas_cache.py`)

```python
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
```
