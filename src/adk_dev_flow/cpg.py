from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CfgNode:
    id: int
    label: str
    line: int
    node_type: str  # 'entry', 'exit', 'statement', 'branch_cond', 'loop_cond', 'call', 'return'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CfgNode:
        return cls(**d)


@dataclass
class CfgEdge:
    source: int
    target: int
    kind: str  # 'next', 'branch_true', 'branch_false', 'loop_body', 'loop_exit', 'jump'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CfgEdge:
        return cls(**d)


@dataclass
class DfgEdge:
    source_var: str
    target_var: str
    source_line: int
    target_line: int
    kind: str  # 'def_use', 'param_pass', 'taint_prop'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DfgEdge:
        return cls(**d)


@dataclass
class CpgEdge:
    source_symbol: str
    target_symbol: str
    edge_kind: str  # 'CALL', 'IMPORT', 'DATA_FLOW', 'REACHING_DEF'
    source_blob: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CpgEdge:
        return cls(**d)


@dataclass
class FileCpg:
    file_path: str
    blob_oid: str
    content_hash: str
    ast_info: Any  # FileAstInfo
    cfg_nodes: list[CfgNode] = field(default_factory=list)
    cfg_edges: list[CfgEdge] = field(default_factory=list)
    dfg_edges: list[DfgEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        ast_dict = self.ast_info.to_dict() if hasattr(self.ast_info, "to_dict") else self.ast_info
        return {
            "file_path": self.file_path,
            "blob_oid": self.blob_oid,
            "content_hash": self.content_hash,
            "ast_info": ast_dict,
            "cfg_nodes": [n.to_dict() for n in self.cfg_nodes],
            "cfg_edges": [e.to_dict() for e in self.cfg_edges],
            "dfg_edges": [d.to_dict() for d in self.dfg_edges],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], ast_info_cls: Any = None) -> FileCpg:
        ast_raw = d.get("ast_info", {})
        if ast_info_cls and hasattr(ast_info_cls, "from_dict") and isinstance(ast_raw, dict):
            ast_info = ast_info_cls.from_dict(ast_raw)
        else:
            ast_info = ast_raw

        return cls(
            file_path=d["file_path"],
            blob_oid=d["blob_oid"],
            content_hash=d["content_hash"],
            ast_info=ast_info,
            cfg_nodes=[CfgNode.from_dict(n) for n in d.get("cfg_nodes", [])],
            cfg_edges=[CfgEdge.from_dict(e) for e in d.get("cfg_edges", [])],
            dfg_edges=[DfgEdge.from_dict(e) for e in d.get("dfg_edges", [])],
        )


def compute_git_blob_oid(data: bytes | str) -> str:
    """Compute exact Git object ID for a blob (100% matches git hash-object)."""
    raw_bytes = data.encode("utf-8") if isinstance(data, str) else data
    header = f"blob {len(raw_bytes)}\0".encode("ascii")
    return hashlib.sha1(header + raw_bytes).hexdigest()


def compute_content_hash(data: bytes | str) -> str:
    """Compute SHA-256 content hash."""
    raw_bytes = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw_bytes).hexdigest()


def _get_call_name(node: ast.AST) -> str | None:
    """Extract symbol name from an ast.Call node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Subscript):
        parent = _get_call_name(node.value)
        if isinstance(node.slice, ast.Constant):
            return f"{parent}[{node.slice.value!r}]" if parent else f"[{node.slice.value!r}]"
    return None


class CpgBuilder:
    """Constructs intra-procedural CFG and DFG from a Python AST."""

    def __init__(self, code: str, file_path: str = "") -> None:
        self.code = code
        self.file_path = file_path
        self.cfg_nodes: list[CfgNode] = []
        self.cfg_edges: list[CfgEdge] = []
        self.dfg_edges: list[DfgEdge] = []
        self._next_node_id = 1

    def _alloc_node(self, label: str, line: int, node_type: str) -> CfgNode:
        node = CfgNode(id=self._next_node_id, label=label, line=line, node_type=node_type)
        self._next_node_id += 1
        self.cfg_nodes.append(node)
        return node

    def build(self, tree: ast.AST) -> tuple[list[CfgNode], list[CfgEdge], list[DfgEdge]]:
        self.cfg_nodes.clear()
        self.cfg_edges.clear()
        self.dfg_edges.clear()
        self._next_node_id = 1

        for item in getattr(tree, "body", []):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._build_function(item)
            elif isinstance(item, ast.ClassDef):
                for m in item.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._build_function(m)
            else:
                self._visit_stmt(item, None)

        return self.cfg_nodes, self.cfg_edges, self.dfg_edges

    def _build_function(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        entry = self._alloc_node(f"entry:{fn.name}", fn.lineno, "entry")
        exit_node = self._alloc_node(f"exit:{fn.name}", getattr(fn, "end_lineno", fn.lineno) or fn.lineno, "exit")

        # Record parameter definitions in DFG
        var_defs: dict[str, int] = {}
        for arg in fn.args.args:
            var_defs[arg.arg] = fn.lineno

        prev = entry
        for stmt in fn.body:
            prev = self._visit_stmt(stmt, prev, var_defs, exit_node)

        if prev is not None and prev != exit_node:
            self.cfg_edges.append(CfgEdge(source=prev.id, target=exit_node.id, kind="next"))

    def _extract_names(self, node: ast.AST) -> set[str]:
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
        return names

    def _trace_call_args(self, node: ast.AST, var_defs: dict[str, int], line: int) -> None:
        """Trace call arguments into DFG as param_pass edges (taint propagation)."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                call_name = _get_call_name(sub.func) or ""
                call_label = f"call:{call_name}@{line}" if call_name else f"call@{line}"
                for arg in sub.args:
                    for u in self._extract_names(arg):
                        if u in var_defs:
                            self.dfg_edges.append(
                                DfgEdge(
                                    source_var=u,
                                    target_var=call_label,
                                    source_line=var_defs[u],
                                    target_line=line,
                                    kind="param_pass",
                                )
                            )

    def _visit_stmt(
        self,
        stmt: ast.AST,
        prev: CfgNode | None,
        var_defs: dict[str, int] | None = None,
        exit_node: CfgNode | None = None,
    ) -> CfgNode | None:
        if var_defs is None:
            var_defs = {}

        line = getattr(stmt, "lineno", 1)

        if isinstance(stmt, ast.If):
            cond_node = self._alloc_node(f"if_cond", line, "branch_cond")
            if prev:
                self.cfg_edges.append(CfgEdge(source=prev.id, target=cond_node.id, kind="next"))

            # Trace DFG on condition variables
            cond_vars = self._extract_names(stmt.test)
            for v in cond_vars:
                if v in var_defs:
                    self.dfg_edges.append(
                        DfgEdge(source_var=v, target_var=f"cond@{line}", source_line=var_defs[v], target_line=line, kind="def_use")
                    )

            # Trace call arguments into DFG (taint reaches flattened condition)
            self._trace_call_args(stmt.test, var_defs, line)

            # True branch
            body_first = True
            curr_true = cond_node
            true_defs = dict(var_defs)
            for s in stmt.body:
                if body_first:
                    s_node = self._visit_stmt(s, None, true_defs, exit_node)
                    if s_node:
                        self.cfg_edges.append(CfgEdge(source=cond_node.id, target=s_node.id, kind="branch_true"))
                        curr_true = s_node
                        body_first = False
                else:
                    curr_true = self._visit_stmt(s, curr_true, true_defs, exit_node)

            # False branch
            curr_false = cond_node
            body_false_first = True
            false_defs = dict(var_defs)
            for s in stmt.orelse:
                if body_false_first:
                    s_node = self._visit_stmt(s, None, false_defs, exit_node)
                    if s_node:
                        self.cfg_edges.append(CfgEdge(source=cond_node.id, target=s_node.id, kind="branch_false"))
                        curr_false = s_node
                        body_false_first = False
                else:
                    curr_false = self._visit_stmt(s, curr_false, false_defs, exit_node)

            if not stmt.orelse:
                # Direct branch_false if no else block
                join_node = self._alloc_node("if_join", getattr(stmt, "end_lineno", line) or line, "statement")
                self.cfg_edges.append(CfgEdge(source=cond_node.id, target=join_node.id, kind="branch_false"))
                if curr_true and curr_true.node_type != "return":
                    self.cfg_edges.append(CfgEdge(source=curr_true.id, target=join_node.id, kind="next"))
                var_defs.update(true_defs)
                return join_node
            else:
                join_node = self._alloc_node("if_join", getattr(stmt, "end_lineno", line) or line, "statement")
                if curr_true and curr_true.node_type != "return":
                    self.cfg_edges.append(CfgEdge(source=curr_true.id, target=join_node.id, kind="next"))
                if curr_false and curr_false.node_type != "return":
                    self.cfg_edges.append(CfgEdge(source=curr_false.id, target=join_node.id, kind="next"))
                var_defs.update(true_defs)
                var_defs.update(false_defs)
                return join_node

        elif isinstance(stmt, (ast.For, ast.While)):
            loop_node = self._alloc_node(f"loop_head", line, "loop_cond")
            if prev:
                self.cfg_edges.append(CfgEdge(source=prev.id, target=loop_node.id, kind="next"))

            # Trace call arguments into DFG (taint reaches flattened loop iteration/condition)
            if isinstance(stmt, ast.For):
                self._trace_call_args(stmt.iter, var_defs, line)
            else:
                self._trace_call_args(stmt.test, var_defs, line)

            curr = loop_node
            loop_first = True
            for s in stmt.body:
                if loop_first:
                    s_node = self._visit_stmt(s, None, var_defs, exit_node)
                    if s_node:
                        self.cfg_edges.append(CfgEdge(source=loop_node.id, target=s_node.id, kind="loop_body"))
                        curr = s_node
                        loop_first = False
                else:
                    curr = self._visit_stmt(s, curr, var_defs, exit_node)

            loop_exit = self._alloc_node(f"loop_exit", getattr(stmt, "end_lineno", line) or line, "statement")
            self.cfg_edges.append(CfgEdge(source=loop_node.id, target=loop_exit.id, kind="loop_exit"))
            if curr and curr.node_type != "return":
                self.cfg_edges.append(CfgEdge(source=curr.id, target=loop_node.id, kind="next"))
            return loop_exit

        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            stmt_node = self._alloc_node(f"assign", line, "statement")
            if prev:
                self.cfg_edges.append(CfgEdge(source=prev.id, target=stmt_node.id, kind="next"))

            # Determine targets
            targets: list[str] = []
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    targets.extend(list(self._extract_names(t)))
                val_node = stmt.value
            elif isinstance(stmt, ast.AnnAssign):
                targets.extend(list(self._extract_names(stmt.target)))
                val_node = stmt.value
            else:  # AugAssign
                targets.extend(list(self._extract_names(stmt.target)))
                val_node = stmt.value

            # Trace call arguments into DFG (taint reaches flattened statements)
            self._trace_call_args(stmt, var_defs, line)

            # Extract source variables
            used_vars = self._extract_names(val_node) if val_node else set()
            for t in targets:
                for u in used_vars:
                    src_line = var_defs.get(u, line)
                    self.dfg_edges.append(
                        DfgEdge(source_var=u, target_var=t, source_line=src_line, target_line=line, kind="def_use")
                    )
                var_defs[t] = line

            return stmt_node

        elif isinstance(stmt, ast.Return):
            ret_node = self._alloc_node("return", line, "return")
            if prev:
                self.cfg_edges.append(CfgEdge(source=prev.id, target=ret_node.id, kind="next"))
            if exit_node:
                self.cfg_edges.append(CfgEdge(source=ret_node.id, target=exit_node.id, kind="jump"))

            # Trace call arguments into DFG (taint reaches flattened statements)
            self._trace_call_args(stmt, var_defs, line)

            if stmt.value:
                used_vars = self._extract_names(stmt.value)
                for u in used_vars:
                    src_line = var_defs.get(u, line)
                    self.dfg_edges.append(
                        DfgEdge(source_var=u, target_var="<return>", source_line=src_line, target_line=line, kind="def_use")
                    )
            return ret_node

        else:
            stmt_node = self._alloc_node("stmt", line, "statement")
            if prev:
                self.cfg_edges.append(CfgEdge(source=prev.id, target=stmt_node.id, kind="next"))
            # Trace call arguments into DFG
            self._trace_call_args(stmt, var_defs, line)
            return stmt_node


class CpgStore:
    """SQLite-based Content-Addressable Storage (CAS) for AST, CFG, and DFG code properties."""

    # Bump this when the cached serialization format changes. A store opened
    # with a different recorded version is fully rebuilt (no row-level migration).
    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                timeout=10.0,
                check_same_thread=False,
            )
            # High-performance, concurrent WAL mode
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cpg_store_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            row = conn.execute(
                "SELECT value FROM cpg_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or row[0] != str(self.SCHEMA_VERSION):
                # Version missing (legacy store) or mismatched: drop everything
                # and rebuild from scratch — no row-level migration.
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS cpg_blob_cache;
                    DROP TABLE IF EXISTS cpg_edges;
                    DROP TABLE IF EXISTS cpg_commit_snapshots;
                    DROP TABLE IF EXISTS cpg_store_meta;
                    """
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cpg_store_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cpg_blob_cache (
                    blob_oid        TEXT PRIMARY KEY,
                    file_path       TEXT NOT NULL,
                    content_hash    TEXT NOT NULL,
                    cpg_json_zlib   BLOB NOT NULL,
                    updated_at      INTEGER DEFAULT (strftime('%s', 'now'))
                );

                CREATE TABLE IF NOT EXISTS cpg_edges (
                    source_symbol TEXT NOT NULL,
                    target_symbol TEXT NOT NULL,
                    edge_kind     TEXT NOT NULL,
                    source_blob   TEXT NOT NULL,
                    PRIMARY KEY (source_symbol, target_symbol, edge_kind)
                );

                CREATE TABLE IF NOT EXISTS cpg_commit_snapshots (
                    commit_oid       TEXT PRIMARY KEY,
                    file_blobs_json  TEXT NOT NULL,
                    indexed_at       INTEGER DEFAULT (strftime('%s', 'now'))
                );

                CREATE INDEX IF NOT EXISTS idx_cpg_edges_source ON cpg_edges (source_symbol);
                CREATE INDEX IF NOT EXISTS idx_cpg_edges_target ON cpg_edges (target_symbol);
                CREATE INDEX IF NOT EXISTS idx_cpg_edges_blob ON cpg_edges (source_blob);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO cpg_store_meta (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    def get_blob_cpg(self, blob_oid: str, ast_info_cls: Any = None) -> FileCpg | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT cpg_json_zlib FROM cpg_blob_cache WHERE blob_oid = ?", (blob_oid,))
        row = cursor.fetchone()
        if not row:
            return None
        try:
            decompressed = zlib.decompress(row[0]).decode("utf-8")
            data = json.loads(decompressed)
            return FileCpg.from_dict(data, ast_info_cls=ast_info_cls)
        except Exception:
            return None

    def put_blob_cpg(self, blob_oid: str, file_path: str, cpg: FileCpg) -> None:
        conn = self._get_conn()
        serialized = json.dumps(cpg.to_dict()).encode("utf-8")
        compressed = zlib.compress(serialized)
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cpg_blob_cache (blob_oid, file_path, content_hash, cpg_json_zlib, updated_at)
                VALUES (?, ?, ?, ?, strftime('%s', 'now'))
                """,
                (blob_oid, file_path, cpg.content_hash, compressed),
            )

    def put_edges(self, edges: list[CpgEdge]) -> None:
        if not edges:
            return
        conn = self._get_conn()
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO cpg_edges (source_symbol, target_symbol, edge_kind, source_blob)
                VALUES (?, ?, ?, ?)
                """,
                [(e.source_symbol, e.target_symbol, e.edge_kind, e.source_blob) for e in edges],
            )

    def get_edges(
        self,
        source_symbol: str | None = None,
        target_symbol: str | None = None,
        edge_kind: str | None = None,
    ) -> list[CpgEdge]:
        conn = self._get_conn()
        query = "SELECT source_symbol, target_symbol, edge_kind, source_blob FROM cpg_edges WHERE 1=1"
        params: list[Any] = []
        if source_symbol:
            query += " AND source_symbol = ?"
            params.append(source_symbol)
        if target_symbol:
            query += " AND target_symbol = ?"
            params.append(target_symbol)
        if edge_kind:
            query += " AND edge_kind = ?"
            params.append(edge_kind)

        cursor = conn.cursor()
        cursor.execute(query, params)
        return [
            CpgEdge(source_symbol=r[0], target_symbol=r[1], edge_kind=r[2], source_blob=r[3])
            for r in cursor.fetchall()
        ]

    def delete_edges_by_source_blob(self, source_blob: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM cpg_edges WHERE source_blob = ?", (source_blob,))

    def get_commit_snapshot(self, commit_oid: str) -> dict[str, str] | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT file_blobs_json FROM cpg_commit_snapshots WHERE commit_oid = ?", (commit_oid,))
        row = cursor.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def put_commit_snapshot(self, commit_oid: str, file_blobs: dict[str, str]) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cpg_commit_snapshots (commit_oid, file_blobs_json, indexed_at)
                VALUES (?, ?, strftime('%s', 'now'))
                """,
                (commit_oid, json.dumps(file_blobs)),
            )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __del__(self) -> None:
        # Release the sqlite file handle deterministically (CPython refcounting)
        # so the db file is not locked after the owner goes out of scope.
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> CpgStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
