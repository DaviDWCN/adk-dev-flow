"""OKF 1.0 — executable contract for OKF typed documents.

OKF (typed YAML frontmatter + markdown body) started as a documentation
convention. This module upgrades it to a machine-enforced contract:

- every OKF type is a Pydantic model, registered in TYPE_REGISTRY;
- ``validate_repo`` schema-checks every typed markdown document in a repo
  and verifies cross-references (ADR supersedes symmetry, run_id against
  .adk/workflow.db);
- ``query`` answers the questions people kept asking by hand: which runs
  lack a spec, which runs went zombie, which ADRs were superseded.

Design principles (evidence-based, from the consumer survey):

- the write/read paths that already exist in code are the contract:
  SpecContract.to_okf_markdown/from_okf_markdown and
  ReviewVerdict.to_okf_markdown/from_okf_markdown define DevSpec and
  ReviewVerdict frontmatter; this module validates that shape, it does
  not re-define it;
- schema validation (structure) is separate from business gates
  (allowed_paths coverage etc. stay in SpecContract's lock-time
  validator);
- unknown extra keys are warnings, not errors (forward compatibility:
  the corpus evolves faster than the registry);
- EngineeringRule frontmatter is OPTIONAL by contract — workflow.py's
  rule router accepts plain markdown rules (meta={}).

CLI: python -m adk_dev_flow.okf validate|query|schema|types
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adk_dev_flow.state_store import StateStore
from adk_dev_flow.subproc import no_window_kwargs

OKF_VERSION = "1.0"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)

# Directories pruned when walking a repo for typed documents.
SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv"}

# Known status domains per type (evidence: producers + corpus). A status
# outside the domain is a WARNING — legal YAML, suspicious provenance.
STATUS_DOMAINS: dict[str, set[str]] = {
    "DevSpec": {"active", "superseded", "archived"},
    "ReviewVerdict": {"passed", "rejected"},
    "EngineeringRule": {"active", "deprecated", "disabled", "inactive"},
    "KnowledgeIndex": {"active", "archived"},
    "AdoptedSpec": {"adopted", "finalized"},
    "ADR": {"proposed", "accepted", "superseded"},
    "ArchitectureDoc": {"active", "superseded", "archived"},
    "Assessment": {"draft", "final", "superseded"},
    "Note": {"draft", "final"},
    "OpsGuide": {"active", "deprecated", "archived"},
    "RequirementMatrix": {"baseline", "active", "archived"},
    "TaskPlan": {"not-started", "in-progress", "done", "archived"},
}


class OkfBase(BaseModel):
    """Common frontmatter contract: every OKF document declares type/title/status."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str = Field(min_length=1)
    status: str = Field(min_length=1)


class DevSpecMeta(OkfBase):
    """DevSpec — pipeline contract document.

    Write/read paths: SpecContract.to_okf_markdown / from_okf_markdown
    (models.py). Structure only here: allowed_paths coverage and test-code
    extraction stay in SpecContract's own validators.
    """

    type: Literal["DevSpec"]
    status: str = "active"
    issue_iid: int | None = None
    target_branch: str = "main"
    base_branch: str | None = None
    branch: str | None = None
    require_tdd_red: bool = True
    spike: bool = False
    extra_dependencies: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(min_length=1)
    acceptance_test_path: str = Field(min_length=1)
    tasks: list[dict[str, Any]] = Field(min_length=1)


class ReviewVerdictMeta(OkfBase):
    """ReviewVerdict — critic audit document.

    Write/read paths: ReviewVerdict.to_okf_markdown / from_okf_markdown.
    Internal consistency (approved <-> status/verified) is enforced here
    because the producers always emit them together.
    """

    type: Literal["ReviewVerdict"]
    # Title is NOT part of the ReviewVerdict contract: the producer
    # (ReviewVerdict.to_okf_markdown) never writes it and the reader never
    # reads it — overriding the OkfBase requirement (corpus-verified).
    title: str = ""  # type: ignore[assignment]
    run_id: str = Field(min_length=1)
    approved: bool
    verified: str = Field(min_length=1)
    status: Literal["passed", "rejected"]
    timestamp: str = Field(min_length=1)
    blocking_issues: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class EngineeringRuleMeta(OkfBase):
    """EngineeringRule — knowledge rule consumed by workflow.py's router.

    The router reads: status (skip deprecated/disabled/inactive), paths or
    match.paths, tags or match.tags. Frontmatter itself is optional for
    rule files; when present, these keys drive selective routing.
    """

    type: Literal["EngineeringRule"]
    status: str = "active"
    paths: list[str] | None = None
    tags: list[str] | None = None
    match: dict[str, list[str]] | None = None
    source_run_id: str | None = None
    timestamp: str | None = None


class KnowledgeIndexMeta(OkfBase):
    type: Literal["KnowledgeIndex"]
    description: str | None = None


class AdoptedSpecMeta(OkfBase):
    """AdoptedSpec — post-hoc fact record for out-of-pipeline work.

    Producer: FlowService._write_adopted_spec_doc (mcp_server.py). Facts
    only, never a pre-hoc contract.
    """

    type: Literal["AdoptedSpec"]
    status: Literal["adopted", "finalized"]
    issue_iid: int
    branch: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    commit_range: str = ""
    evidence_type: Literal["post_hoc"]
    run_id: str = Field(min_length=1)
    adopted_at: str = Field(min_length=1)


class AdrMeta(OkfBase):
    """ADR — architecture decision record.

    MADR 0.2 comparison: MADR carries decisionDate/status (frontmatter)
    and context/decision/alternatives (body). We keep exactly the
    indexable fields in frontmatter — date/status plus the decision-GRAPH
    edges (supersedes/partial/closes_issue/related_issues) — and leave
    narrative content (context/decision/alternatives) in the body, where
    MADR also puts it. Field names match MADR where they overlap
    (status); the graph edges are our extension.
    """

    type: Literal["ADR"]
    adr: int
    status: str = "accepted"
    date: str = Field(min_length=1)
    baseline: str | None = None
    supersedes: list[int] = Field(default_factory=list)
    supersedes_partial: list[int] = Field(default_factory=list)
    superseded_by: list[int] = Field(default_factory=list)
    superseded_by_partial: list[int] = Field(default_factory=list)
    closes_issue: int | None = None
    related_issues: list[int] = Field(default_factory=list)


class ArchitectureDocMeta(OkfBase):
    type: Literal["ArchitectureDoc"]
    date: str = Field(min_length=1)
    component: str | None = None


class AssessmentMeta(OkfBase):
    type: Literal["Assessment"]
    date: str = Field(min_length=1)
    topic: str | None = None


class NoteMeta(OkfBase):
    type: Literal["Note"]
    date: str = Field(min_length=1)
    related_issues: list[int] = Field(default_factory=list)
    feeds_adr: list[int] = Field(default_factory=list)


class OpsGuideMeta(OkfBase):
    type: Literal["OpsGuide"]
    date: str = Field(min_length=1)
    stage: str | None = None
    related_issues: list[int] = Field(default_factory=list)
    related_adrs: list[int] = Field(default_factory=list)


class RequirementMatrixMeta(OkfBase):
    type: Literal["RequirementMatrix"]
    date: str = Field(min_length=1)
    phase: int | None = None


class TaskPlanMeta(OkfBase):
    type: Literal["TaskPlan"]
    date: str = Field(min_length=1)
    phase: int | None = None
    milestone: str | None = None


TYPE_REGISTRY: dict[str, type[OkfBase]] = {
    m.model_fields["type"].annotation.__args__[0]: m  # type: ignore[union-attr]
    for m in (
        DevSpecMeta,
        ReviewVerdictMeta,
        EngineeringRuleMeta,
        KnowledgeIndexMeta,
        AdoptedSpecMeta,
        AdrMeta,
        ArchitectureDocMeta,
        AssessmentMeta,
        NoteMeta,
        OpsGuideMeta,
        RequirementMatrixMeta,
        TaskPlanMeta,
    )
}


class OkfIssue:
    """A single validation finding."""

    __slots__ = ("level", "path", "message")

    def __init__(self, level: str, path: str, message: str) -> None:
        self.level = level  # "error" | "warning" | "info"
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "path": self.path, "message": self.message}


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Split a markdown document into (frontmatter dict, body, error)."""
    if not text.startswith("---"):
        return None, text, None
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, text, "frontmatter 存在但闭合 '---' 缺失或与正文粘连（需要独立成行）"
    try:
        meta = yaml.safe_load(match.group(1))
    except Exception as exc:
        return None, text, f"YAML frontmatter 解析失败: {exc}"
    if not isinstance(meta, dict):
        return None, text, "YAML frontmatter 必须为字典对象"
    return meta, text[match.end():], None


def _iter_markdown_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    skip_parts = {".git", "__pycache__", "node_modules", ".venv"}
    for path in sorted(repo_root.rglob("*.md")):
        rel = path.relative_to(repo_root).as_posix()
        parts = rel.split("/")
        if any(p in skip_parts for p in parts):
            continue
        if rel.startswith(".adk/worktrees/"):
            continue
        files.append(path)
    return files


def _is_git_commit(repo_root: Path, sha: str) -> bool:
    """Return True iff sha resolves to an existing commit object in repo_root."""
    clean = sha.strip()
    if not re.match(r"^[0-9a-fA-F]{4,64}$", clean):
        return False
    try:
        res = subprocess.run(
            ["git", "cat-file", "-t", clean],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            **no_window_kwargs(),
        )
        return res.returncode == 0 and res.stdout.strip() == "commit"
    except Exception:
        return False


def _has_git_provenance(repo_root: Path, rel: str, shas: list[str]) -> bool:
    """Verify an OKF audit/adopted record committed on another machine via Git history.

    Requires BOTH that the record itself is committed at HEAD:<rel> and that at least
    one recorded provenance/head commit SHA resolves to a real commit in this repo.
    """
    if not (repo_root / ".git").exists():
        return False
    candidates = [str(s).strip() for s in shas if s and str(s).strip()]
    if not candidates:
        return False
    try:
        in_head = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            **no_window_kwargs(),
        )
        if in_head.returncode != 0:
            return False
    except Exception:
        return False
    return any(_is_git_commit(repo_root, sha) for sha in candidates)


def _check_run_cross_reference(
    repo_root: Path,
    rel: str,
    rid: str,
    shas: list[str],
    run_ids: set[str] | None,
    issues: list[OkfIssue],
) -> None:
    """Validate run_id against local DB, falling back to Git provenance for committed records."""
    if run_ids is None or rid in run_ids:
        return
    if _has_git_provenance(repo_root, rel, shas):
        issues.append(
            OkfIssue(
                "info",
                rel,
                f"run_id '{rid}' 不在本地 .adk/workflow.db，但已通过 Git 提交与 provenance 溯源校验",
            )
        )
        return
    issues.append(OkfIssue("error", rel, f"跨引用失败: run_id '{rid}' 不存在于 .adk/workflow.db"))


def _load_run_ids(db_path: Path) -> set[str] | None:
    """Return run_ids in the local workflow DB, or None when there is none."""
    if not db_path.is_file():
        return None
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT run_id FROM runs").fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    return {str(r[0]) for r in rows}


def validate_repo(repo_root: Path) -> dict[str, Any]:
    """Validate every typed markdown document under repo_root.

    Errors: contract violations (missing required keys, internal
    contradictions, broken cross-references). Warnings: unknown types,
    unknown status values, extra keys beyond the schema (forward
    compatibility). Info: optional-frontmatter situations.
    """
    repo_root = Path(repo_root)
    issues: list[OkfIssue] = []
    type_counts: dict[str, int] = {}
    run_ids = _load_run_ids(repo_root / ".adk" / "workflow.db")
    adr_files: dict[int, Path] = {}

    # First pass: collect ADR numbers for the graph consistency check.
    md_files = _iter_markdown_files(repo_root)
    parsed: list[tuple[Path, dict[str, Any], str]] = []
    for path in md_files:
        rel = path.relative_to(repo_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            issues.append(OkfIssue("error", rel, f"文件不可读: {exc}"))
            continue
        meta, _body, fm_err = split_frontmatter(text)
        if fm_err:
            issues.append(OkfIssue("error", rel, fm_err))
            continue
        if meta is None:
            continue  # plain markdown without frontmatter: out of OKF scope
        doc_type = str(meta.get("type") or "")
        if doc_type == "ADR":
            try:
                adr_files[int(meta.get("adr"))] = path
            except (TypeError, ValueError):
                pass
        parsed.append((path, meta, rel))

    for path, meta, rel in parsed:
        doc_type = str(meta.get("type") or "")
        if not doc_type:
            if "name" in meta:
                # Loader-contract frontmatter (SKILL.md name/description): not OKF scope.
                issues.append(OkfIssue("info", rel, "有 frontmatter 但无 'type'（加载器契约文件，如 SKILL.md），跳过"))
            else:
                issues.append(OkfIssue("warning", rel, "frontmatter 缺少 'type' 键，无法归类"))
            continue
        model = TYPE_REGISTRY.get(doc_type)
        if model is None:
            issues.append(OkfIssue("warning", rel, f"未知 OKF 类型: '{doc_type}'（不在 OKF {OKF_VERSION} 注册表）"))
            continue
        type_counts[doc_type] = type_counts.get(doc_type, 0) + 1

        # Schema validation.
        try:
            model.model_validate(meta)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or doc_type
                issues.append(OkfIssue("error", rel, f"schema 校验失败 [{doc_type}.{loc}]: {err['msg']}"))
            continue

        # Unknown extra keys: forward-compat warnings.
        known = set(model.model_fields.keys())
        extras = sorted(set(meta.keys()) - known)
        if extras:
            issues.append(OkfIssue("warning", rel, f"frontmatter 含未注册键: {', '.join(extras)}"))

        # Status domain (warning-level: legal YAML, unknown provenance).
        status = str(meta.get("status", ""))
        domain = STATUS_DOMAINS.get(doc_type)
        if domain and status not in domain:
            issues.append(OkfIssue("warning", rel, f"status '{status}' 不在 {doc_type} 已知值域 {sorted(domain)}"))

        # Type-specific cross-reference checks.
        if doc_type == "DevSpec":
            for idx, task in enumerate(meta.get("tasks") or []):
                if not isinstance(task, dict):
                    issues.append(OkfIssue("error", rel, f"tasks[{idx}] 必须为对象"))
                    continue
                for key in ("id", "title", "description"):
                    if not str(task.get(key) or "").strip():
                        issues.append(OkfIssue("error", rel, f"tasks[{idx}].{key} 不能为空（消费者 AtomicTask 要求）"))
                tf = task.get("target_files")
                if not isinstance(tf, list) or not tf:
                    issues.append(OkfIssue("error", rel, f"tasks[{idx}].target_files 必须为非空列表（消费者 AtomicTask 要求）"))
        elif doc_type == "ReviewVerdict":
            approved = bool(meta.get("approved"))
            if approved != (meta.get("status") == "passed"):
                issues.append(OkfIssue("error", rel, "内部矛盾: approved 与 status(passed/rejected) 不一致"))
            expected_verified = "process:acceptance_gate_passed" if approved else "process:acceptance_gate_failed"
            if meta.get("verified") != expected_verified:
                issues.append(OkfIssue("error", rel, f"内部矛盾: verified 应为 '{expected_verified}'"))
            rid = str(meta.get("run_id") or "")
            prov = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else {}
            _check_run_cross_reference(
                repo_root,
                rel,
                rid,
                [
                    str(prov.get("base_sha") or ""),
                    str(prov.get("locked_test_commit_sha") or ""),
                ],
                run_ids,
                issues,
            )
        elif doc_type == "AdoptedSpec":
            rid = str(meta.get("run_id") or "")
            _check_run_cross_reference(
                repo_root,
                rel,
                rid,
                [str(meta.get("head_sha") or "")],
                run_ids,
                issues,
            )
        elif doc_type == "ADR":
            adr_no = int(meta.get("adr"))
            forward = [(t, False) for t in meta.get("supersedes", [])] + [
                (t, True) for t in meta.get("supersedes_partial", [])
            ]
            for target, partial in forward:
                tpath = adr_files.get(int(target))
                if tpath is None:
                    issues.append(OkfIssue("warning", rel, f"supersedes 引用的 ADR {target} 在仓库中不存在"))
                    continue
                tmeta, _b, terr = split_frontmatter(tpath.read_text(encoding="utf-8"))
                back_key = "superseded_by_partial" if partial else "superseded_by"
                if terr or not tmeta:
                    issues.append(OkfIssue("warning", rel, f"被引用的 ADR {target} frontmatter 异常，无法核对回链"))
                    continue
                back = [int(x) for x in (tmeta.get(back_key) or [])]
                if adr_no not in back:
                    issues.append(
                        OkfIssue(
                            "error",
                            rel,
                            f"supersedes 对称性破坏: ADR {adr_no} 声明{'部分' if partial else ''}取代 "
                            f"ADR {target}，但 ADR {target} 的 {back_key} 未回指 {adr_no}",
                        )
                    )

    summary = {
        "okf_version": OKF_VERSION,
        "repo_root": str(repo_root),
        "files_scanned": len(md_files),
        "typed_documents": sum(type_counts.values()),
        "type_counts": dict(sorted(type_counts.items())),
        "errors": [i.as_dict() for i in issues if i.level == "error"],
        "warnings": [i.as_dict() for i in issues if i.level == "warning"],
        "infos": [i.as_dict() for i in issues if i.level == "info"],
        "valid": not any(i.level == "error" for i in issues),
    }
    return summary


# ---------------------------------------------------------------------------
# Query presets — the questions this session kept answering by hand.
# ---------------------------------------------------------------------------

def _get_store(repo_root: Path) -> StateStore | None:
    db_path = repo_root / ".adk" / "workflow.db"
    if not db_path.is_file():
        return None
    return StateStore(repo_root)


def query_spec_coverage(repo_root: Path) -> dict[str, Any]:
    """Runs declaring an issue vs spec documents on disk (both directions)."""
    repo_root = Path(repo_root)
    store = _get_store(repo_root)
    specs_dir = repo_root / ".adk" / "specs"
    spec_issues: set[int] = set()
    if specs_dir.is_dir():
        for f in specs_dir.glob("issue-*.md"):
            m = re.match(r"issue-(\d+)", f.name)
            if m:
                spec_issues.add(int(m.group(1)))
    run_issues: dict[int, list[dict[str, str]]] = {}
    if store is not None:
        for run in store.list_runs(limit=1000):
            iid = run.get("issue_iid")
            if iid is None:
                continue
            run_issues.setdefault(int(iid), []).append(
                {"run_id": run["run_id"], "status": run["status"]}
            )
    missing_spec = sorted(set(run_issues) - spec_issues)
    orphan_specs = sorted(spec_issues - set(run_issues))
    return {
        "query": "spec-coverage",
        "issues_with_runs": len(run_issues),
        "issues_with_spec_docs": len(spec_issues),
        "runs_without_spec": [
            {"issue_iid": iid, "runs": run_issues[iid]} for iid in missing_spec
        ],
        "orphan_spec_docs": orphan_specs,
    }


def query_zombie_runs(repo_root: Path, threshold_hours: float = 24.0) -> dict[str, Any]:
    """ACTIVE runs whose updated_at is older than the threshold."""
    store = _get_store(Path(repo_root))
    if store is None:
        return {"query": "zombie-runs", "threshold_hours": threshold_hours, "zombies": []}
    now = datetime.now(timezone.utc)
    zombies: list[dict[str, str]] = []
    for run in store.get_active_runs():
        raw = str(run.get("updated_at") or "")
        try:
            updated = datetime.fromisoformat(raw)
        except ValueError:
            zombies.append({"run_id": run["run_id"], "status": run["status"], "issue_iid": run.get("issue_iid"), "reason": "updated_at 不可解析"})
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        idle_hours = (now - updated).total_seconds() / 3600
        if idle_hours > threshold_hours:
            zombies.append(
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "issue_iid": run.get("issue_iid"),
                    "idle_hours": round(idle_hours, 1),
                }
            )
    return {"query": "zombie-runs", "threshold_hours": threshold_hours, "zombies": zombies}


def query_adr_superseded(repo_root: Path) -> dict[str, Any]:
    """ADR decision-graph report: full/partial supersedes edges and their backlinks."""
    repo_root = Path(repo_root)
    edges: list[dict[str, Any]] = []
    for path in sorted((repo_root / "docs" / "adr").glob("*.md")) if (repo_root / "docs" / "adr").is_dir() else []:
        meta, _body, err = split_frontmatter(path.read_text(encoding="utf-8"))
        if err or not meta or meta.get("type") != "ADR":
            continue
        edges.append(
            {
                "adr": meta.get("adr"),
                "status": meta.get("status"),
                "supersedes": meta.get("supersedes", []),
                "supersedes_partial": meta.get("supersedes_partial", []),
                "superseded_by": meta.get("superseded_by", []),
                "superseded_by_partial": meta.get("superseded_by_partial", []),
            }
        )
    return {"query": "adr-superseded", "adrs": edges}


def run_query(repo_root: Path, preset: str, threshold_hours: float = 24.0) -> dict[str, Any]:
    presets = {
        "spec-coverage": lambda: query_spec_coverage(repo_root),
        "zombie-runs": lambda: query_zombie_runs(repo_root, threshold_hours),
        "adr-superseded": lambda: query_adr_superseded(repo_root),
        "coverage": lambda: {
            "query": "coverage",
            **{
                k: v
                for k, v in validate_repo(Path(repo_root)).items()
                if k in ("type_counts", "typed_documents", "okf_version")
            },
        },
    }
    fn = presets.get(preset)
    if fn is None:
        return {"error": f"未知查询预设: '{preset}'，可用: {sorted(presets)}"}
    return fn()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="okf", description=f"OKF {OKF_VERSION} 可执行契约工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验仓库内全部 OKF 类型化文档")
    p_validate.add_argument("path", nargs="?", default=".", help="仓库根（默认当前目录）")
    p_validate.add_argument("--json", action="store_true", help="输出 JSON")

    p_query = sub.add_parser("query", help="运行查询预设")
    p_query.add_argument("preset", choices=["spec-coverage", "zombie-runs", "adr-superseded", "coverage"])
    p_query.add_argument("path", nargs="?", default=".")
    p_query.add_argument("--threshold-hours", type=float, default=24.0)

    p_schema = sub.add_parser("schema", help="输出某类型的 JSON Schema")
    p_schema.add_argument("type", nargs="?", default=None, help="类型名（缺省列出全部）")

    sub.add_parser("types", help="列出注册的全部 OKF 类型")

    args = parser.parse_args(argv)

    if args.command == "validate":
        report = validate_repo(Path(args.path))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"OKF {OKF_VERSION} 校验: {report['repo_root']}")
            print(f"  扫描 {report['files_scanned']} 个 markdown，{report['typed_documents']} 个类型化文档")
            for kind, label in (("errors", "错误"), ("warnings", "警告")):
                for issue in report[kind]:
                    print(f"  [{label}] {issue['path']}: {issue['message']}")
            print("  => " + ("有效" if report["valid"] else f"无效（{len(report['errors'])} 个错误）"))
        return 0 if report["valid"] else 1

    if args.command == "query":
        print(json.dumps(run_query(Path(args.path), args.preset, args.threshold_hours), ensure_ascii=False, indent=2))
        return 0

    if args.command == "schema":
        if args.type:
            model = TYPE_REGISTRY.get(args.type)
            if model is None:
                print(f"未知类型: '{args.type}'，可用: {sorted(TYPE_REGISTRY)}", file=sys.stderr)
                return 1
            print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
            return 0
        for name, model in sorted(TYPE_REGISTRY.items()):
            print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "types":
        for name in sorted(TYPE_REGISTRY):
            print(name)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
