from __future__ import annotations

import ast
import os
from pathlib import Path
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from adk_dev_flow.cpg import (
    CfgEdge,
    CfgNode,
    CpgBuilder,
    CpgEdge,
    CpgStore,
    DfgEdge,
    FileCpg,
    compute_content_hash,
    compute_git_blob_oid,
    iter_arg_names,
)


class SymbolKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"


@dataclass
class SymbolDef:
    name: str
    kind: SymbolKind
    line_start: int
    line_end: int
    signature: str
    docstring: str | None = None
    decorators: list[str] = field(default_factory=list)
    calls: set[str] = field(default_factory=set)
    parameters: list[str] = field(default_factory=list)
    param_defaults: dict[str, str] = field(default_factory=dict)
    return_annotation: str | None = None
    is_local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value if isinstance(self.kind, SymbolKind) else str(self.kind),
            "line_start": self.line_start,
            "line_end": self.line_end,
            "signature": self.signature,
            "docstring": self.docstring,
            "decorators": list(self.decorators),
            "calls": sorted(list(self.calls)),
            "parameters": list(self.parameters),
            "param_defaults": dict(self.param_defaults),
            "return_annotation": self.return_annotation,
            "is_local": self.is_local,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SymbolDef:
        kind_raw = d.get("kind", "function")
        try:
            kind = SymbolKind(kind_raw)
        except Exception:
            kind = SymbolKind.FUNCTION
        return cls(
            name=d["name"],
            kind=kind,
            line_start=d.get("line_start", 1),
            line_end=d.get("line_end", 1),
            signature=d.get("signature", ""),
            docstring=d.get("docstring"),
            decorators=d.get("decorators", []),
            calls=set(d.get("calls", [])),
            parameters=d.get("parameters", []),
            param_defaults=d.get("param_defaults", {}),
            return_annotation=d.get("return_annotation"),
            is_local=d.get("is_local", False),
        )


@dataclass
class FileAstInfo:
    file_path: str  # Relative to repo_root (e.g., 'src/adk_dev_flow/models.py')
    imports: list[str] = field(default_factory=list)
    internal_imports: list[str] = field(default_factory=list)
    definitions: dict[str, SymbolDef] = field(default_factory=dict)
    syntax_valid: bool = True
    syntax_error: str | None = None
    mtime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "imports": list(self.imports),
            "internal_imports": list(self.internal_imports),
            "definitions": {k: v.to_dict() for k, v in self.definitions.items()},
            "syntax_valid": self.syntax_valid,
            "syntax_error": self.syntax_error,
            "mtime": self.mtime,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileAstInfo:
        defs = {k: SymbolDef.from_dict(v) for k, v in d.get("definitions", {}).items()}
        return cls(
            file_path=d.get("file_path", ""),
            imports=d.get("imports", []),
            internal_imports=d.get("internal_imports", []),
            definitions=defs,
            syntax_valid=d.get("syntax_valid", True),
            syntax_error=d.get("syntax_error"),
            mtime=d.get("mtime", 0.0),
        )


@dataclass
class ImpactReport:
    target_paths: list[str]
    impacted_production_files: list[str] = field(default_factory=list)
    impacted_test_files: list[str] = field(default_factory=list)
    suggested_allowed_paths: list[str] = field(default_factory=list)
    impacted_callers: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_paths": self.target_paths,
            "impacted_production_files": self.impacted_production_files,
            "impacted_test_files": self.impacted_test_files,
            "suggested_allowed_paths": self.suggested_allowed_paths,
            "impacted_callers": self.impacted_callers,
            "summary": self.summary,
        }


@dataclass
class AstDiffReport:
    file_path: str
    syntax_ok: bool = True
    syntax_error: str | None = None
    breaking_issues: list[str] = field(default_factory=list)
    dangerous_nodes: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return (not self.syntax_ok) or bool(self.breaking_issues) or bool(self.dangerous_nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "syntax_ok": self.syntax_ok,
            "syntax_error": self.syntax_error,
            "breaking_issues": self.breaking_issues,
            "dangerous_nodes": self.dangerous_nodes,
            "has_issues": self.has_issues,
        }


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


class SymbolVisitor(ast.NodeVisitor):
    """Walk an AST to collect definitions and references."""

    def __init__(self, source_lines: list[str]) -> None:
        self.source_lines = source_lines
        self.definitions: dict[str, SymbolDef] = {}
        self.current_class: str | None = None
        self.current_symbol: SymbolDef | None = None
        self.scope_stack: list[SymbolDef] = []

    def _extract_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
        start = max(0, node.lineno - 1)
        # Scan until colon or up to 5 lines
        sig_lines: list[str] = []
        for line in self.source_lines[start : min(len(self.source_lines), start + 8)]:
            sig_lines.append(line.strip())
            if line.rstrip().endswith(":"):
                break
        return " ".join(sig_lines)

    def _extract_decorators(self, decorator_list: list[ast.expr]) -> list[str]:
        decs: list[str] = []
        for d in decorator_list:
            if isinstance(d, ast.Name):
                decs.append(d.id)
            elif isinstance(d, ast.Attribute):
                decs.append(f"{_get_call_name(d.value) or ''}.{d.attr}")
            elif isinstance(d, ast.Call):
                name = _get_call_name(d.func)
                if name:
                    decs.append(name)
        return decs

    def _extract_params(self, args: ast.arguments) -> tuple[list[str], dict[str, str]]:
        params: list[str] = []
        defaults: dict[str, str] = {}

        # Positional / keyword args
        num_defaults = len(args.defaults)
        non_default_count = len(args.args) - num_defaults

        for i, a in enumerate(args.args):
            params.append(a.arg)
            if i >= non_default_count:
                def_idx = i - non_default_count
                try:
                    defaults[a.arg] = ast.unparse(args.defaults[def_idx])
                except Exception:
                    defaults[a.arg] = "..."

        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            params.append(a.arg)
            if d is not None:
                try:
                    defaults[a.arg] = ast.unparse(d)
                except Exception:
                    defaults[a.arg] = "..."

        if args.vararg:
            params.append(f"*{args.vararg.arg}")
        if args.kwarg:
            params.append(f"**{args.kwarg.arg}")

        return params, defaults

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        name = node.name
        is_local = any(
            s.kind in (SymbolKind.FUNCTION, SymbolKind.ASYNC_FUNCTION)
            for s in self.scope_stack
        )
        if is_local and self.scope_stack:
            full_name = f"{self.scope_stack[-1].name}.<locals>.{name}"
        elif self.current_class:
            full_name = f"{self.current_class}.{name}"
        else:
            full_name = name

        doc = ast.get_docstring(node)
        sig = self._extract_signature(node)
        decs = self._extract_decorators(node.decorator_list)

        sym = SymbolDef(
            name=full_name,
            kind=SymbolKind.CLASS,
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            signature=sig,
            docstring=doc,
            decorators=decs,
            is_local=is_local,
        )
        self.definitions[full_name] = sym

        prev_class = self.current_class
        prev_sym = self.current_symbol
        self.current_class = full_name
        self.current_symbol = sym
        self.scope_stack.append(sym)

        self.generic_visit(node)

        self.scope_stack.pop()
        self.current_class = prev_class
        self.current_symbol = prev_sym

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        name = node.name
        is_local = any(
            s.kind in (SymbolKind.FUNCTION, SymbolKind.ASYNC_FUNCTION)
            for s in self.scope_stack
        )
        if self.scope_stack and (
            is_local or self.scope_stack[-1].kind in (SymbolKind.FUNCTION, SymbolKind.ASYNC_FUNCTION)
        ):
            full_name = f"{self.scope_stack[-1].name}.<locals>.{name}"
            is_local = True
            kind = SymbolKind.ASYNC_FUNCTION if is_async else SymbolKind.FUNCTION
        elif self.current_class:
            full_name = f"{self.current_class}.{name}"
            kind = SymbolKind.METHOD
        else:
            full_name = name
            kind = SymbolKind.ASYNC_FUNCTION if is_async else SymbolKind.FUNCTION

        doc = ast.get_docstring(node)
        sig = self._extract_signature(node)
        decs = self._extract_decorators(node.decorator_list)
        params, defaults = self._extract_params(node.args)

        ret_annot: str | None = None
        if node.returns:
            try:
                ret_annot = ast.unparse(node.returns)
            except Exception:
                ret_annot = None

        sym = SymbolDef(
            name=full_name,
            kind=kind,
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            signature=sig,
            docstring=doc,
            decorators=decs,
            parameters=params,
            param_defaults=defaults,
            return_annotation=ret_annot,
            is_local=is_local,
        )
        self.definitions[full_name] = sym

        prev_sym = self.current_symbol
        self.current_symbol = sym
        self.scope_stack.append(sym)

        self.generic_visit(node)

        self.scope_stack.pop()
        self.current_symbol = prev_sym

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node, is_async=True)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _get_call_name(node.func)
        if call_name:
            if self.current_symbol:
                self.current_symbol.calls.add(call_name)
            for sym in self.scope_stack:
                sym.calls.add(call_name)
        self.generic_visit(node)


class CodeGraph:
    """In-memory Code & Dependency Graph for Python repositories."""

    DEFAULT_IGNORE_DIRS = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        "dist",
        "build",
        ".adk",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }

    def __init__(self, repo_root: Path | str, store: CpgStore | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        if store is None:
            # Default: enable CAS storage under the repo-local cache directory
            # (created on demand) so sync_git / analyze_impact hit the cache.
            store = CpgStore(self.repo_root / ".adk" / "cache" / "cpg_cache.db")
        self.store = store
        self.file_index: dict[str, FileAstInfo] = {}
        self.file_cpgs: dict[str, FileCpg] = {}
        # file_path -> set of file_paths that import it
        self.reverse_file_deps: dict[str, set[str]] = {}
        # symbol_name -> file_path
        self.symbol_to_file: dict[str, str] = {}
        # symbol_name -> set of caller symbol names
        self.reverse_symbol_calls: dict[str, set[str]] = {}

    def close(self) -> None:
        """Close backing CPG store if open."""
        if self.store is not None:
            self.store.close()

    def __enter__(self) -> CodeGraph:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def scan_file_cpg(
        self,
        rel_path: str,
        code: str | None = None,
        blob_oid: str | None = None,
    ) -> tuple[FileAstInfo, FileCpg]:
        """Parse a single python file and extract AST + intra-procedural CFG and DFG.

        Leverages CpgStore CAS cache when blob_oid is provided or computed.
        """
        full_path = self.repo_root / rel_path
        if code is None:
            if not full_path.exists() or not full_path.is_file():
                info = FileAstInfo(file_path=rel_path, syntax_valid=False, syntax_error="文件不存在")
                cpg = FileCpg(file_path=rel_path, blob_oid="", content_hash="", ast_info=info)
                return info, cpg
            try:
                code = full_path.read_text(encoding="utf-8", errors="replace")
                mtime = full_path.stat().st_mtime
            except Exception as e:
                info = FileAstInfo(file_path=rel_path, syntax_valid=False, syntax_error=str(e))
                cpg = FileCpg(file_path=rel_path, blob_oid="", content_hash="", ast_info=info)
                return info, cpg
        else:
            mtime = 0.0

        if not blob_oid:
            blob_oid = compute_git_blob_oid(code)

        # 1. Check CAS cache in store
        if self.store is not None:
            cached_cpg = self.store.get_blob_cpg(blob_oid, ast_info_cls=FileAstInfo)
            if cached_cpg is not None:
                cached_cpg.file_path = rel_path
                ast_info = cached_cpg.ast_info
                if isinstance(ast_info, FileAstInfo):
                    ast_info.file_path = rel_path
                    ast_info.mtime = mtime
                    return ast_info, cached_cpg

        # 2. Parse AST
        try:
            tree = ast.parse(code, filename=rel_path)
        except SyntaxError as e:
            info = FileAstInfo(
                file_path=rel_path,
                syntax_valid=False,
                syntax_error=f"SyntaxError at line {e.lineno}: {e.msg}",
                mtime=mtime,
            )
            cpg = FileCpg(
                file_path=rel_path,
                blob_oid=blob_oid,
                content_hash=compute_content_hash(code),
                ast_info=info,
            )
            return info, cpg

        source_lines = code.splitlines()
        visitor = SymbolVisitor(source_lines)
        visitor.visit(tree)

        imports: list[str] = []
        internal_imports: list[str] = []

        # Find all import statements
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    full_import = f"{mod}.{alias.name}" if mod else alias.name
                    imports.append(full_import)

        info = FileAstInfo(
            file_path=rel_path,
            imports=imports,
            internal_imports=internal_imports,
            definitions=visitor.definitions,
            syntax_valid=True,
            mtime=mtime,
        )

        # 3. Build CFG and DFG
        builder = CpgBuilder(code, file_path=rel_path)
        cfg_nodes, cfg_edges, dfg_edges = builder.build(tree)

        content_hash = compute_content_hash(code)
        cpg = FileCpg(
            file_path=rel_path,
            blob_oid=blob_oid,
            content_hash=content_hash,
            ast_info=info,
            cfg_nodes=cfg_nodes,
            cfg_edges=cfg_edges,
            dfg_edges=dfg_edges,
        )

        # 4. Save to CAS store
        if self.store is not None:
            self.store.put_blob_cpg(blob_oid, rel_path, cpg)

        return info, cpg

    def scan_file(self, rel_path: str, code: str | None = None) -> FileAstInfo:
        """Parse a single python file and extract AST metadata (delegates to scan_file_cpg)."""
        info, cpg = self.scan_file_cpg(rel_path, code=code)
        self.file_cpgs[rel_path] = cpg
        return info

    def _build_dependencies(self) -> None:
        """Resolve imports, build reverse dependencies and reverse symbol calls."""
        self.reverse_file_deps.clear()
        self.symbol_to_file.clear()
        self.reverse_symbol_calls.clear()

        # Build symbol_to_file
        for rel, info in self.file_index.items():
            for sym_name in info.definitions.keys():
                self.symbol_to_file[sym_name] = rel

        # Build module-to-file resolver map
        module_to_file: dict[str, str] = {}
        for rel in self.file_index.keys():
            no_ext = rel[:-3] if rel.endswith(".py") else rel
            dotted = no_ext.replace("/", ".").replace("\\", ".")
            module_to_file[dotted] = rel
            if dotted.startswith("src."):
                module_to_file[dotted[4:]] = rel

        # Resolve imports and build reverse dependencies
        for rel, info in self.file_index.items():
            info.internal_imports.clear()
            for imp in info.imports:
                matched_file: str | None = None
                if imp in module_to_file:
                    matched_file = module_to_file[imp]
                else:
                    parts = imp.split(".")
                    for i in range(len(parts) - 1, 0, -1):
                        prefix = ".".join(parts[:i])
                        if prefix in module_to_file:
                            matched_file = module_to_file[prefix]
                            break

                if matched_file and matched_file != rel:
                    info.internal_imports.append(matched_file)
                    self.reverse_file_deps.setdefault(matched_file, set()).add(rel)

            # Build reverse symbol calls
            for sym_name, sym in info.definitions.items():
                for called in sym.calls:
                    target_sym = called
                    if target_sym not in self.symbol_to_file:
                        matches = [s for s in self.symbol_to_file if s.endswith(f".{called}") or s == called]
                        if len(matches) == 1:
                            target_sym = matches[0]

                    if target_sym in self.symbol_to_file:
                        self.reverse_symbol_calls.setdefault(target_sym, set()).add(sym_name)

    def index_repo(self, subdirs: list[str] | None = None) -> None:
        """Scan and build code graph across the repository."""
        self.file_index.clear()
        self.file_cpgs.clear()

        dirs_to_scan = [self.repo_root / s for s in subdirs] if subdirs else [self.repo_root]

        py_files: list[Path] = []
        for d in dirs_to_scan:
            if not d.exists():
                continue
            for root, dirs, files in os.walk(d):
                dirs[:] = [name for name in dirs if name not in self.DEFAULT_IGNORE_DIRS]
                for file in files:
                    if file.endswith(".py"):
                        py_files.append(Path(root) / file)

        # 1. Parse all files
        for p in py_files:
            try:
                rel = p.resolve().relative_to(self.repo_root).as_posix()
            except ValueError:
                rel = p.as_posix()
            info, cpg = self.scan_file_cpg(rel)
            self.file_index[rel] = info
            self.file_cpgs[rel] = cpg

        # 2. Build dependency relationships
        self._build_dependencies()

    def sync_git(self, commit_sha: str | None = None) -> dict[str, int]:
        """Incrementally synchronize AST and CPG graph with Git repository tree.

        Uses git ls-tree to retrieve exact Git Blob OIDs.
        Unchanged blobs hit the CAS cache (0 AST parsing overhead, <15ms).
        Only modified or new blobs trigger AST/CFG/DFG extraction.
        """
        import subprocess
        import sys

        # Windows: launch git child processes without a console window.
        # On POSIX the creationflags key is not passed at all.
        run_kwargs: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        )

        # 0. Guard: repo_root must be a git repository root (i.e. contain .git).
        # A plain directory, or a sub-directory of a repo (where ls-tree output
        # would be relative to that sub-directory), is not syncable via the git
        # tree — fall back to a full working-tree index instead.
        if not (self.repo_root / ".git").exists():
            self.index_repo()
            return {"parsed_files": len(self.file_index), "cached_hits": 0, "total_files": len(self.file_index)}

        # 1. Resolve commit SHA
        if not commit_sha:
            try:
                res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    check=True,
                    **run_kwargs,
                )
                commit_sha = res.stdout.decode("ascii").strip()
            except Exception:
                self.index_repo()
                return {"parsed_files": len(self.file_index), "cached_hits": 0, "total_files": len(self.file_index)}

        # 2. Run git ls-tree -r -z <commit_sha>
        try:
            res = subprocess.run(
                ["git", "ls-tree", "-r", "-z", commit_sha],
                cwd=str(self.repo_root),
                capture_output=True,
                check=True,
                **run_kwargs,
            )
            raw_output = res.stdout
        except Exception:
            self.index_repo()
            return {"parsed_files": len(self.file_index), "cached_hits": 0, "total_files": len(self.file_index)}

        # 3. Parse tree entries
        parsed_files = 0
        cached_hits = 0
        current_tree_blobs: dict[str, str] = {}

        entries = raw_output.split(b"\0")
        current_repo_files: set[str] = set()

        for entry in entries:
            if not entry:
                continue
            try:
                meta, path_bytes = entry.split(b"\t", 1)
                rel_path = path_bytes.decode("utf-8", errors="replace").replace("\\", "/")
                parts = meta.split(b" ")
                if len(parts) >= 3 and parts[1] == b"blob":
                    blob_oid = parts[2].decode("ascii")
                else:
                    continue
            except Exception:
                continue

            # Only process python files and ignore ignored directories
            if not rel_path.endswith(".py"):
                continue
            path_parts = set(rel_path.split("/"))
            if path_parts.intersection(self.DEFAULT_IGNORE_DIRS):
                continue

            current_repo_files.add(rel_path)
            current_tree_blobs[rel_path] = blob_oid

            # Check CAS cache
            cached_cpg = self.store.get_blob_cpg(blob_oid, ast_info_cls=FileAstInfo)
            if cached_cpg is not None:
                cached_hits += 1
                cached_cpg.file_path = rel_path
                ast_info = cached_cpg.ast_info
                if isinstance(ast_info, FileAstInfo):
                    ast_info.file_path = rel_path
                    self.file_index[rel_path] = ast_info
                self.file_cpgs[rel_path] = cached_cpg
            else:
                # Cache miss: fetch the committed content from the git object
                # store via cat-file. Never read the working tree — dirty
                # content must not enter the cache under a clean blob OID
                # (cache poisoning). On cat-file failure degrade to "" so this
                # file simply fails to parse instead of poisoning the cache.
                parsed_files += 1
                try:
                    cat_res = subprocess.run(
                        ["git", "cat-file", "-p", blob_oid],
                        cwd=str(self.repo_root),
                        capture_output=True,
                        check=True,
                        **run_kwargs,
                    )
                    code = cat_res.stdout.decode("utf-8", errors="replace")
                except Exception:
                    code = ""

                info, cpg = self.scan_file_cpg(rel_path, code=code, blob_oid=blob_oid)
                self.file_index[rel_path] = info
                self.file_cpgs[rel_path] = cpg

        # Remove deleted files from index
        to_remove = [f for f in self.file_index if f not in current_repo_files]
        for f in to_remove:
            self.file_index.pop(f, None)
            self.file_cpgs.pop(f, None)

        # Save snapshot
        self.store.put_commit_snapshot(commit_sha, current_tree_blobs)

        # Rebuild dependency relations
        self._build_dependencies()

        return {
            "parsed_files": parsed_files,
            "cached_hits": cached_hits,
            "total_files": len(current_repo_files),
        }

    def trace_data_flow(
        self,
        source_var: str,
        target_var: str,
        rel_path: str | None = None,
    ) -> list[DfgEdge]:
        """Find data flow path (chain of DfgEdges) from source_var to target_var."""
        cpgs_to_check = [self.file_cpgs[rel_path]] if rel_path and rel_path in self.file_cpgs else list(self.file_cpgs.values())
        for cpg in cpgs_to_check:
            adj: dict[str, list[DfgEdge]] = {}
            for edge in cpg.dfg_edges:
                adj.setdefault(edge.source_var, []).append(edge)

            queue: list[tuple[str, list[DfgEdge]]] = [(source_var, [])]
            visited = {source_var}
            while queue:
                curr, path = queue.pop(0)
                if curr == target_var or target_var in curr:
                    return path
                for edge in adj.get(curr, []):
                    nxt = edge.target_var
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append((nxt, path + [edge]))
        return []

    def analyze_impact(self, target_paths_or_symbols: list[str]) -> ImpactReport:
        """Compute the blast radius / impacted files and tests for modified paths/symbols."""
        if not self.file_index:
            if self.store is not None:
                # First visit goes through sync_git (CAS-backed incremental sync);
                # it internally falls back to index_repo for non-git repos or
                # when git is unavailable.
                self.sync_git()
            else:
                self.index_repo()

        matched_files: set[str] = set()
        matched_symbols: set[str] = set()

        for item in target_paths_or_symbols:
            clean = item.strip().replace("\\", "/")
            # Check if file path
            if clean in self.file_index:
                matched_files.add(clean)
            elif (self.repo_root / clean).resolve() in [self.repo_root / f for f in self.file_index]:
                matched_files.add(clean)
            else:
                # Try prefix/substring match on file
                matching_files = [f for f in self.file_index if f == clean or f.endswith(f"/{clean}")]
                if matching_files:
                    matched_files.update(matching_files)
                elif clean in self.symbol_to_file:
                    matched_symbols.add(clean)
                    matched_files.add(self.symbol_to_file[clean])

        # Find impacted files via imports (1-hop and 2-hop)
        impacted_files: set[str] = set(matched_files)
        direct_dependents: set[str] = set()

        for f in matched_files:
            deps = self.reverse_file_deps.get(f, set())
            direct_dependents.update(deps)
            impacted_files.update(deps)

        # 2-hop dependents
        for d in direct_dependents:
            impacted_files.update(self.reverse_file_deps.get(d, set()))

        # Find impacted callers via reverse symbol calls
        impacted_callers: set[str] = set()
        for sym in matched_symbols:
            callers = self.reverse_symbol_calls.get(sym, set())
            impacted_callers.update(callers)
            for c in callers:
                if c in self.symbol_to_file:
                    impacted_files.add(self.symbol_to_file[c])

        # Also find all symbols defined in matched_files
        for f in matched_files:
            info = self.file_index.get(f)
            if info:
                for sym_name in info.definitions:
                    callers = self.reverse_symbol_calls.get(sym_name, set())
                    impacted_callers.update(callers)
                    for c in callers:
                        if c in self.symbol_to_file:
                            impacted_files.add(self.symbol_to_file[c])

        # Classify into production files and test files
        production_files: list[str] = []
        test_files: list[str] = []

        for f in sorted(impacted_files):
            fname = Path(f).name
            if f.startswith("tests/") or "/tests/" in f or fname.startswith("test_") or fname.endswith("_test.py"):
                test_files.append(f)
            else:
                production_files.append(f)

        # Suggested allowed_paths: original targets + all impacted production files + all impacted test files
        suggested_allowed = sorted(list(matched_files | set(production_files) | set(test_files)))

        summary = (
            f"目标路径包含 {len(matched_files)} 个核心文件与 {len(matched_symbols)} 个符号；"
            f"AST 分析共检测到 {len(production_files)} 个关联生产文件与 {len(test_files)} 个受影响测试套件，"
            f"识别出 {len(impacted_callers)} 个直接调用点。"
        )

        return ImpactReport(
            target_paths=list(target_paths_or_symbols),
            impacted_production_files=production_files,
            impacted_test_files=test_files,
            suggested_allowed_paths=suggested_allowed,
            impacted_callers=sorted(list(impacted_callers)),
            summary=summary,
        )

    def get_test_generation_context(self, target_paths_or_symbols: list[str]) -> dict[str, Any]:
        """Generate structured context specifically tailored for Test Agents.

        Provides:
        1. 'impact': ImpactReport (impacted production, test files, callers).
        2. 'target_skeletons': Type-annotated skeletons for target files (no function bodies).
        3. 'impacted_caller_skeletons': Signatures of callers that invoke symbols in target files.
        4. 'impacted_tests_info': Existing test functions within the impacted test files.
        """
        report = self.analyze_impact(target_paths_or_symbols)

        target_skeletons: dict[str, str] = {}
        for f in report.target_paths:
            norm = f.strip().replace("\\", "/")
            matching = [p for p in self.file_index if p == norm or p.endswith(f"/{norm}")]
            for m in matching:
                p = self.repo_root / m
                if p.is_file():
                    try:
                        code = p.read_text(encoding="utf-8", errors="replace")
                        target_skeletons[m] = generate_skeleton(code, file_path=m)
                    except Exception:
                        pass

        caller_skeletons: dict[str, str] = {}
        for caller in report.impacted_callers:
            if caller in self.symbol_to_file:
                c_file = self.symbol_to_file[caller]
                info = self.file_index.get(c_file)
                if info and caller in info.definitions:
                    sym = info.definitions[caller]
                    caller_skeletons[caller] = f"{c_file}:{sym.line_start} {sym.signature}"

        impacted_tests_info: dict[str, list[str]] = {}
        for tf in report.impacted_test_files:
            info = self.file_index.get(tf)
            if info:
                test_funcs = [
                    name
                    for name in info.definitions
                    if name.startswith("test_") or ".test_" in name
                ]
                impacted_tests_info[tf] = test_funcs

        return {
            "impact": report.to_dict(),
            "target_skeletons": target_skeletons,
            "impacted_caller_skeletons": caller_skeletons,
            "impacted_tests_info": impacted_tests_info,
        }


def generate_skeleton(code: str, file_path: str = "") -> str:
    """Generate a clean type-stub skeleton by replacing function/method bodies with Ellipsis (...)."""
    try:
        tree = ast.parse(code, filename=file_path)
    except SyntaxError:
        return code

    class SkeletonTransformer(ast.NodeTransformer):
        def _transform_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
            doc = ast.get_docstring(node)
            # Recursively preserve nested FunctionDef, AsyncFunctionDef, or ClassDef
            nested_defs: list[ast.stmt] = []
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    nested_defs.append(self.visit(stmt))

            new_body: list[ast.stmt] = []
            if doc:
                new_body.append(ast.Expr(value=ast.Constant(value=doc)))
            new_body.extend(nested_defs)
            new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
            node.body = new_body
            return node

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            return self._transform_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
            return self._transform_func(node)

    new_tree = SkeletonTransformer().visit(tree)
    ast.fix_missing_locations(new_tree)
    try:
        return ast.unparse(new_tree)
    except Exception:
        return code


def audit_ast_diff(
    base_code: str,
    new_code: str,
    file_path: str = "",
    allowed_dangerous_calls: set[str] | Sequence[str] | None = None,
) -> AstDiffReport:
    """Perform deterministic AST-level semantic audit between base code and modified code."""
    # 1. Syntax check on new code
    try:
        new_tree = ast.parse(new_code, filename=file_path)
    except SyntaxError as e:
        return AstDiffReport(
            file_path=file_path,
            syntax_ok=False,
            syntax_error=f"SyntaxError at line {e.lineno}: {e.msg}",
        )

    # Collect line suppression tags (# noqa: adk-ast / # noqa)
    suppressed_lines: set[int] = set()
    for idx, line in enumerate(new_code.splitlines(), 1):
        if re.search(r"#\s*noqa(?::\s*adk(?:-ast)?)?", line, re.IGNORECASE):
            suppressed_lines.add(idx)
            if line.strip().startswith("#"):
                suppressed_lines.add(idx + 1)

    allowed_calls = set(allowed_dangerous_calls or [])

    # 2. Syntax check on base code (if empty, treat as new file)
    base_tree: ast.AST | None = None
    if base_code.strip():
        try:
            base_tree = ast.parse(base_code, filename=file_path)
        except SyntaxError:
            base_tree = None

    report = AstDiffReport(file_path=file_path, syntax_ok=True)

    # 3. Dangerous node patterns inspection
    dangerous_calls = {
        "os.system",
        "subprocess.Popen",
        "subprocess.run",
        "subprocess.call",
        "eval",
        "exec",
        "__import__",
    }

    def _is_node_suppressed(node: ast.AST) -> bool:
        lineno = getattr(node, "lineno", -1)
        return lineno in suppressed_lines

    for node in ast.walk(new_tree):
        if _is_node_suppressed(node):
            continue

        if isinstance(node, ast.Call):
            call_name = _get_call_name(node.func) or ""
            if call_name in allowed_calls:
                continue
            if call_name in dangerous_calls or any(
                f"[{d!r}]" in call_name for d in ("eval", "exec", "system", "popen")
            ):
                report.dangerous_nodes.append(f"发现危险函数调用 `{call_name}` (line {node.lineno})")
            elif call_name == "getattr" and len(node.args) >= 2:
                target_mod = _get_call_name(node.args[0]) or ""
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                    if attr_arg.value in ("system", "popen", "eval", "exec", "spawn", "__builtins__"):
                        report.dangerous_nodes.append(
                            f"发现反射危险函数调用 `getattr({target_mod}, '{attr_arg.value}')` (line {node.lineno})"
                        )
                elif isinstance(attr_arg, (ast.BinOp, ast.JoinedStr, ast.Call)):
                    if target_mod in ("os", "subprocess", "builtins", "__builtins__", "sys"):
                        report.dangerous_nodes.append(
                            f"发现敏感模块上的动态反射调用 `getattr({target_mod}, <dynamic>)` (line {node.lineno})"
                        )
            elif call_name in ("globals", "locals", "vars", "__import__"):
                report.dangerous_nodes.append(
                    f"发现全局作用域反射或内建提权访问 `{call_name}()` (line {node.lineno})"
                )
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            report.dangerous_nodes.append(f"发现内建提权命名空间访问 `__builtins__` (line {node.lineno})")
        elif isinstance(node, ast.Constant) and node.value == "__builtins__":
            report.dangerous_nodes.append(f"发现敏感内建属性索引 `'__builtins__'` (line {node.lineno})")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("pty", "shelve"):
                    report.dangerous_nodes.append(f"发现敏感模块导入 `{alias.name}` (line {node.lineno})")
        elif isinstance(node, ast.Assign):
            # Check for suspicious hardcoded secret token patterns or decoded credentials
            for target in node.targets:
                if isinstance(target, ast.Name):
                    target_lower = target.id.lower()
                    if any(k in target_lower for k in ("secret", "token", "password", "api_key")):
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            val = node.value.value
                            if len(val) >= 16 and not val.startswith("env:") and not val.startswith("$"):
                                report.dangerous_nodes.append(
                                    f"疑似硬编码凭据 `{target.id} = '***'` (line {node.lineno})"
                                )
                        elif isinstance(node.value, ast.Call):
                            val_fn = _get_call_name(node.value.func) or ""
                            if any(
                                kw in val_fn.lower()
                                for kw in ("b64decode", "decode", "fromhex", "decrypt")
                            ):
                                report.dangerous_nodes.append(
                                    f"疑似混淆还原敏感凭据 `{target.id} = {val_fn}(...)` (line {node.lineno})"
                                )

    # 3b. CPG DFG-based taint flow analysis.
    # Taint is scoped per top-level function or class method: sources,
    # adjacency table and sink checks are confined to a single callable's
    # subgraph, so same-name parameters of different functions/methods cannot
    # cross-talk through a file-level graph. Module-level code has no formal
    # parameters and yields no taint.
    def _collect_callable_scopes(
        stmts: list[ast.stmt], *, in_class: bool = False
    ) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
        scopes: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool]] = []
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((stmt, in_class))
            elif isinstance(stmt, ast.ClassDef):
                scopes.extend(_collect_callable_scopes(stmt.body, in_class=True))
        return scopes

    try:
        for fn_node, is_method in _collect_callable_scopes(new_tree.body):
            fn_module = ast.Module(body=[fn_node], type_ignores=[])
            builder = CpgBuilder(new_code, file_path=file_path)
            _, _, dfg_edges = builder.build(fn_module)

            adj: dict[str, list[DfgEdge]] = {}
            for edge in dfg_edges:
                adj.setdefault(edge.source_var, []).append(edge)

            # Formal parameters defined within this callable scope are the
            # only taint sources (nested defs stay inside this subgraph).
            param_sources: list[str] = []
            for item in ast.walk(fn_node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg_name in iter_arg_names(item.args):
                        if is_method and item is fn_node and arg_name in ("self", "cls"):
                            continue
                        if arg_name not in param_sources:
                            param_sources.append(arg_name)

            # Trace reachable targets from parameters within this scope
            for p in param_sources:
                visited = {p}
                queue = [p]
                while queue:
                    curr = queue.pop(0)
                    for edge in adj.get(curr, []):
                        nxt = edge.target_var
                        abs_line = edge.target_line
                        if abs_line in suppressed_lines:
                            continue
                        if any(d in nxt for d in ("call:eval", "call:exec", "call:os.system", "call:subprocess")):
                            report.dangerous_nodes.append(
                                f"检测到污点变量 `{p}` 流入危险函数 `{nxt}` (line {abs_line})"
                            )
                        if nxt not in visited:
                            visited.add(nxt)
                            queue.append(nxt)
    except Exception as exc:
        raise RuntimeError(f"AST audit internal error in {file_path}: {exc}") from exc

    # 4. Breaking change detection against base code
    if base_tree:
        base_visitor = SymbolVisitor(base_code.splitlines())
        base_visitor.visit(base_tree)

        new_visitor = SymbolVisitor(new_code.splitlines())
        new_visitor.visit(new_tree)

        # Check for deleted public symbols
        for sym_name, sym in base_visitor.definitions.items():
            # Only care about public API (not starting with _, not local closure)
            if getattr(sym, "is_local", False) or "<locals>" in sym_name:
                continue
            short_name = sym_name.split(".")[-1]
            if short_name.startswith("_"):
                continue

            if sym_name not in new_visitor.definitions:
                report.breaking_issues.append(f"删除了公共符号 `{sym_name}`")
            else:
                new_sym = new_visitor.definitions[sym_name]
                if getattr(new_sym, "is_local", False) or "<locals>" in sym_name:
                    continue
                if new_sym.line_start in suppressed_lines:
                    continue

                # Check for removed parameters
                base_params = [p for p in sym.parameters if not p.startswith("*")]
                new_params = [p for p in new_sym.parameters if not p.startswith("*")]

                # Check if old required parameter was removed
                for p in base_params:
                    if p not in new_params and p != "self" and p != "cls":
                        report.breaking_issues.append(
                            f"符号 `{sym_name}` 移除了参数 `{p}` (破坏向后兼容性)"
                        )

                # Check if old optional parameter had its default removed (making it required)
                for p in base_params:
                    if p in new_params and p in sym.param_defaults and p not in new_sym.param_defaults:
                        report.breaking_issues.append(
                            f"符号 `{sym_name}` 将原有可选参数 `{p}` 改为无默认值的必填参数 (破坏向后兼容性)"
                        )

                # Check if new required parameter was added without a default
                for p in new_params:
                    if p not in base_params and p != "self" and p != "cls":
                        if p not in new_sym.param_defaults:
                            report.breaking_issues.append(
                                f"符号 `{sym_name}` 新增了无默认值的必填参数 `{p}` (可能导致调用方崩溃)"
                            )

    return report


def cli_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="adk-dev-flow-impact",
        description="AST-powered code graph and impact radius analyzer for Python repositories.",
    )
    parser.add_argument("targets", nargs="+", help="Target file paths or symbol names to analyze.")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root directory (default: current directory).",
    )
    args = parser.parse_args(argv)

    cg = CodeGraph(args.repo_root)
    cg.index_repo()
    report = cg.analyze_impact(args.targets)

    print("=" * 60)
    print("🔍 ADK AST 代码图谱与影响面分析报告")
    print("=" * 60)
    print(f"📌 目标路径/符号: {', '.join(report.target_paths)}")
    print(f"\n📦 关联生产文件 ({len(report.impacted_production_files)}):")
    for f in report.impacted_production_files:
        print(f"  - {f}")
    print(f"\n🧪 受影响测试套件 ({len(report.impacted_test_files)}):")
    for f in report.impacted_test_files:
        print(f"  - {f}")
    print(f"\n🔗 受波及调用点 ({len(report.impacted_callers)}):")
    for c in report.impacted_callers[:15]:
        print(f"  - {c}")
    if len(report.impacted_callers) > 15:
        print(f"  ... 另有 {len(report.impacted_callers) - 15} 个调用点")
    print(f"\n🛡️ 建议 allowed_paths ({len(report.suggested_allowed_paths)}):")
    for f in report.suggested_allowed_paths:
        print(f"  - {f}")
    print("\n" + "=" * 60)
    print(f"💡 {report.summary}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(cli_main())

