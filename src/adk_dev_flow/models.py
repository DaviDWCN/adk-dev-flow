from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
from typing import Any

from pydantic import BaseModel, Field, model_validator
import yaml

from adk_dev_flow.guard import is_path_under, is_protected_path, strip_dot_slash
from adk_dev_flow.state_store import RunStatus, StateStore

__all__ = [
    "AtomicTask",
    "ReviewVerdict",
    "RunStatus",
    "SpecContract",
    "StateStore",
]


def _validate_repo_relative_path(raw: str, field_name: str) -> str:
    cleaned = raw.strip().replace("\\", "/")
    if not cleaned:
        raise ValueError(f"{field_name} 不能为空路径")
    if cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":"):
        raise ValueError(f"{field_name} 必须为相对路径，禁止绝对路径: {raw}")
    parts = PurePosixPath(cleaned).parts
    if ".." in parts:
        raise ValueError(f"{field_name} 禁止包含路径穿越 '..': {raw}")
    norm = strip_dot_slash(cleaned).rstrip("/")
    if not norm or norm == ".":
        raise ValueError(f"{field_name} 不能指向仓库根目录 '.'")
    if is_protected_path(norm):
        raise ValueError(f"{field_name} 禁止包含受保护的门禁/框架路径: {raw}")
    return norm


class AtomicTask(BaseModel):
    id: str = Field(description="任务唯一ID，如 T1")
    title: str
    description: str
    target_files: list[str] = Field(min_length=1, description="允许修改的相对路径列表")


class SpecContract(BaseModel):
    issue_iid: int | None = None
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    target_branch: str = "main"
    base_branch: str | None = Field(
        default=None,
        description="拉取施工 Worktree 的基线分支（用于 Stacked 分支；缺省使用 target_branch）",
    )
    branch: str | None = Field(
        default=None,
        description="自定义开发分支名（缺省使用系统生成的 adk/issue-* 分支）",
    )
    allowed_paths: list[str] = Field(min_length=1, description="允许修改的目录或文件前缀白名单")
    acceptance_test_path: str = Field(
        description="锁定的验收测试文件相对路径，如 tests/test_issue_42.py"
    )
    acceptance_test_code: str = Field(min_length=1, description="完整可运行的 pytest 验收测试源码")
    require_tdd_red: bool = Field(
        default=True,
        description="是否强制要求验收测试在基线代码上执行失败（TDD Red 校验，防范空断言或假通过测试）",
    )
    extra_dependencies: list[str] = Field(
        default_factory=list,
        description="任务执行所需增量安装的第三方 Python 依赖（如 ['httpx>=0.27', 'pydantic-settings']）",
    )
    tasks: list[AtomicTask] = Field(min_length=1)
    design_doc: str | None = Field(
        default=None,
        description="设计 Agent 产出的详细架构方案、数据契约及设计决策",
    )
    impact_analysis: dict[str, Any] | None = Field(
        default=None,
        description="AST 代码图谱影响面分析结果（关联生产文件、测试集及受波及调用点）",
    )
    red_evidence: dict[str, Any] | None = Field(
        default=None,
        description="基线 TDD 红灯预检执行客观凭证（退出码、错误摘要、判定）",
    )
    pre_critic_report: dict[str, Any] | None = Field(
        default=None,
        description="前置 Critic 对设计与测试一致性及合规性的审查报告",
    )

    @model_validator(mode="after")
    def _validate_contract_integrity(self) -> SpecContract:
        self.allowed_paths = [
            _validate_repo_relative_path(p, "allowed_paths") for p in self.allowed_paths
        ]
        self.acceptance_test_path = _validate_repo_relative_path(
            self.acceptance_test_path, "acceptance_test_path"
        )

        # Static syntax check so a broken acceptance test never burns worker repair retries.
        try:
            ast.parse(self.acceptance_test_code, filename=self.acceptance_test_path)
        except SyntaxError as exc:
            raise ValueError(
                f"acceptance_test_code 存在 Python 语法错误 (line {exc.lineno}): {exc.msg}"
            ) from exc

        for task in self.tasks:
            normalized_targets: list[str] = []
            for tf in task.target_files:
                norm_tf = _validate_repo_relative_path(tf, f"tasks[{task.id}].target_files")
                if norm_tf == self.acceptance_test_path:
                    raise ValueError(
                        f"任务 {task.id} 不得将锁定的验收测试文件作为目标修改文件: {norm_tf}"
                    )
                if not any(is_path_under(norm_tf, allowed) for allowed in self.allowed_paths):
                    raise ValueError(
                        f"任务 {task.id} 的目标文件 `{norm_tf}` 未被 allowed_paths {self.allowed_paths} 覆盖"
                    )
                normalized_targets.append(norm_tf)
            task.target_files = normalized_targets

        return self

    def to_okf_markdown(self) -> str:
        """Serialize this SpecContract into an OKF-compliant Markdown document with YAML frontmatter."""
        meta: dict[str, Any] = {
            "type": "DevSpec",
            "title": self.title,
            "status": "active",
            "issue_iid": self.issue_iid,
            "target_branch": self.target_branch,
            "require_tdd_red": self.require_tdd_red,
            "extra_dependencies": self.extra_dependencies,
            "allowed_paths": self.allowed_paths,
            "acceptance_test_path": self.acceptance_test_path,
            "tasks": [t.model_dump() for t in self.tasks],
        }
        if self.base_branch:
            meta["base_branch"] = self.base_branch
        if self.branch:
            meta["branch"] = self.branch
        yaml_header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()

        tasks_md = "\n\n".join(
            f"### Task {t.id}: {t.title}\n{t.description}\n- **Target Files**: {', '.join(t.target_files)}"
            for t in self.tasks
        )

        content = (
            f"---\n"
            f"{yaml_header}\n"
            f"---\n\n"
            f"# {self.title}\n\n"
            f"## Summary\n"
            f"{self.summary}\n\n"
        )
        if self.design_doc:
            content += f"## Architecture Design\n\n{self.design_doc.strip()}\n\n"

        content += (
            f"## Tasks Breakdown\n\n"
            f"{tasks_md}\n\n"
            f"## Locked Acceptance Test (`{self.acceptance_test_path}`)\n\n"
            f"```python\n"
            f"{self.acceptance_test_code.strip()}\n"
            f"```\n"
        )

        if self.red_evidence:
            exit_code = self.red_evidence.get("exit_code", "N/A")
            summary = (
                self.red_evidence.get("assertion_error_summary")
                or self.red_evidence.get("summary")
                or "Red validated"
            )
            content += (
                f"\n## TDD 基线红灯凭证 (Pre-flight Red Evidence)\n\n"
                f"- **基线执行退出码**: `{exit_code}`\n"
                f"- **红灯有效性**: `{'纯净业务断言红灯' if self.red_evidence.get('is_pure_semantic_failure') else '需关注'}`\n"
                f"- **错误摘要**: `{summary}`\n"
            )

        if self.pre_critic_report:
            score = self.pre_critic_report.get("consistency_score", "N/A")
            status = "APPROVED" if self.pre_critic_report.get("approved") else "CHANGES_REQUESTED"
            summary = self.pre_critic_report.get("summary") or "一致性预审已完成"
            content += (
                f"\n## 前置 Critic 审查报告 (Pre-Critic Review)\n\n"
                f"- **前置审查结论**: `{status}`\n"
                f"- **设计-测试契约一致性打分**: `{score}`\n"
                f"- **审查摘要**: {summary}\n"
            )

        return content

    @classmethod
    def from_okf_markdown(cls, text_or_path: str | Path) -> SpecContract:
        """Parse an OKF-compliant Markdown document into a validated SpecContract."""
        if isinstance(text_or_path, Path):
            raw_text = text_or_path.read_text(encoding="utf-8")
        elif "\n" not in str(text_or_path) and Path(str(text_or_path)).is_file():
            raw_text = Path(str(text_or_path)).read_text(encoding="utf-8")
        else:
            raw_text = str(text_or_path)

        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$", raw_text, re.DOTALL)
        if not match:
            raise ValueError("无效的 OKF 文档：缺少以 '---' 包裹的 YAML Frontmatter")

        frontmatter_str, body_str = match.group(1), match.group(2)
        try:
            meta = yaml.safe_load(frontmatter_str) or {}
        except Exception as exc:
            raise ValueError(f"YAML Frontmatter 解析失败: {exc}") from exc

        if not isinstance(meta, dict):
            raise ValueError("YAML Frontmatter 必须为字典对象")

        doc_type = meta.get("type")
        if doc_type != "DevSpec":
            raise ValueError(f"无效的 OKF 概念类型: expected 'DevSpec', got '{doc_type}'")

        # Extract acceptance test code from code blocks in markdown body
        code_match = re.search(r"```(?:python|py)?\s*\n(.*?)\n```", body_str, re.DOTALL)
        if code_match:
            acceptance_test_code = code_match.group(1).strip()
        else:
            acceptance_test_code = meta.get("acceptance_test_code", "")

        if not acceptance_test_code:
            raise ValueError("OKF 文档中未找到有效的 pytest 验收测试代码块 (```python ... ```)")

        # Summary can come from meta or extracted from body
        summary = meta.get("summary")
        if not summary:
            sum_match = re.search(r"## Summary\s*\n(.*?)(?=\n##|\Z)", body_str, re.DOTALL)
            summary = sum_match.group(1).strip() if sum_match else meta.get("title", "")

        tasks_raw = meta.get("tasks") or []
        tasks = [AtomicTask.model_validate(t) for t in tasks_raw]
        if not tasks:
            raise ValueError("OKF 规约中缺少任务列表 (tasks)")

        return cls(
            issue_iid=meta.get("issue_iid"),
            title=meta.get("title") or "Untitled OKF Spec",
            summary=summary,
            target_branch=meta.get("target_branch", "main"),
            base_branch=meta.get("base_branch"),
            branch=meta.get("branch"),
            allowed_paths=meta.get("allowed_paths", []),
            acceptance_test_path=meta.get("acceptance_test_path", ""),
            acceptance_test_code=acceptance_test_code,
            require_tdd_red=meta.get("require_tdd_red", True),
            extra_dependencies=meta.get("extra_dependencies", []),
            tasks=tasks,
        )


class ReviewVerdict(BaseModel):
    approved: bool
    confidence: float = Field(default=1.0, description="置信度评分 (0.0~1.0)")
    blocking_issues: list[str] = Field(default_factory=list)
    summary: str

    def to_okf_markdown(
        self,
        run_id: str,
        provenance: dict[str, Any] | None = None,
        raw_report: str | None = None,
    ) -> str:
        """Serialize ReviewVerdict into an OKF-compliant audit document."""
        meta: dict[str, Any] = {
            "type": "ReviewVerdict",
            "run_id": run_id,
            "approved": self.approved,
            "verified": (
                "process:acceptance_gate_passed"
                if self.approved
                else "process:acceptance_gate_failed"
            ),
            "status": "passed" if self.approved else "rejected",
            "blocking_issues": self.blocking_issues,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provenance": provenance or {},
        }
        yaml_header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()

        issues_md = (
            "\n".join(f"- {iss}" for iss in self.blocking_issues)
            if self.blocking_issues
            else "- 无阻碍性问题 (All checks passed)"
        )

        content = (
            f"---\n"
            f"{yaml_header}\n"
            f"---\n\n"
            f"# Critic Review Verdict: {'APPROVED' if self.approved else 'REJECTED'}\n\n"
            f"## Summary\n"
            f"{self.summary}\n\n"
            f"## Blocking Issues\n"
            f"{issues_md}\n\n"
        )
        if raw_report:
            content += f"## Detailed Critic Report\n\n{raw_report.strip()}\n"
        return content

    @classmethod
    def from_okf_markdown(cls, text_or_path: str | Path) -> ReviewVerdict:
        """Parse an OKF-compliant ReviewVerdict document."""
        if isinstance(text_or_path, Path):
            raw_text = text_or_path.read_text(encoding="utf-8")
        elif "\n" not in str(text_or_path) and Path(str(text_or_path)).is_file():
            raw_text = Path(str(text_or_path)).read_text(encoding="utf-8")
        else:
            raw_text = str(text_or_path)

        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$", raw_text, re.DOTALL)
        if not match:
            raise ValueError("无效的 OKF 文档：缺少以 '---' 包裹的 YAML Frontmatter")

        frontmatter_str, body_str = match.group(1), match.group(2)
        try:
            meta = yaml.safe_load(frontmatter_str) or {}
        except Exception as exc:
            raise ValueError(f"YAML Frontmatter 解析失败: {exc}") from exc

        if not isinstance(meta, dict) or meta.get("type") != "ReviewVerdict":
            raise ValueError(f"无效的 OKF 概念类型: expected 'ReviewVerdict'")

        summary = meta.get("summary")
        if not summary:
            sum_match = re.search(r"## Summary\s*\n(.*?)(?=\n##|\Z)", body_str, re.DOTALL)
            summary = sum_match.group(1).strip() if sum_match else ""

        return cls(
            approved=bool(meta.get("approved")),
            blocking_issues=meta.get("blocking_issues") or [],
            summary=summary or "Parsed from OKF",
        )
