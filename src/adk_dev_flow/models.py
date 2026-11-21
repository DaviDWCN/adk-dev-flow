from __future__ import annotations

import ast
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator

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


class ReviewVerdict(BaseModel):
    approved: bool
    blocking_issues: list[str] = Field(default_factory=list)
    summary: str
