from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from adk_dev_flow.dashboard import get_dashboard_html
from adk_dev_flow.forge_client import BaseGitForge, GitLabClient, get_git_forge
from adk_dev_flow.models import SpecContract
from adk_dev_flow.state_store import PRE_LOCK_STATUSES, StateStore
from adk_dev_flow.subproc import run_subprocess_safe
from adk_dev_flow.workflow import execute_workflow_run


def parse_epic_into_subtasks(epic_text: str, default_labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Parse a macro epic/PRD requirement string into discrete atomic sub-issues."""
    cleaned = epic_text.strip()
    if not cleaned:
        return []

    labels = list(default_labels or [])
    subtasks: list[dict[str, Any]] = []

    # 1. Try matching numbered list patterns, e.g. "1. xxx; 2. yyy" or lines "1. xxx\n2. yyy"
    pattern = re.compile(r"(?:^|\n|\s*)(?:(\d+)[\.\、\)]|\-|\*)\s*([^\n;；]+(?:[;；]|\n|$))", re.MULTILINE)
    matches = pattern.findall(cleaned)
    if len(matches) >= 2:
        for idx, (num, item_text) in enumerate(matches, 1):
            text = item_text.strip(" ;；\n\r\t")
            if not text:
                continue
            title = text if len(text) <= 50 else f"{text[:47]}..."
            if not any(title.startswith(pfx) for pfx in ("feat", "fix", "refactor", "chore", "test", "docs")):
                title = f"feat: {title}"
            subtasks.append({
                "index": idx,
                "title": title,
                "description": (
                    f"### 需求背景\n来自宏观需求拆解：\n> {cleaned}\n\n"
                    f"### 本任务目标\n{text}\n\n"
                    f"### 验收标准 (Acceptance Criteria)\n"
                    f"- [ ] 编写针对该功能的验收测试用例\n"
                    f"- [ ] 通过红灯验真与 AST 变异测试 (击杀率 ≥ 85%)\n"
                    f"- [ ] 独立沙箱 Worktree 编码施工并通过全量自动化测试\n"
                    f"- [ ] AST 语义审计无破坏性公共签名变更\n"
                ),
                "labels": labels,
            })
    else:
        # 2. Try splitting by semicolon or newlines
        parts = [p.strip() for p in re.split(r"[;；\n]+", cleaned) if p.strip()]
        if len(parts) >= 2:
            for idx, part in enumerate(parts, 1):
                title = part if len(part) <= 50 else f"{part[:47]}..."
                if not any(title.startswith(pfx) for pfx in ("feat", "fix", "refactor", "chore", "test", "docs")):
                    title = f"feat: {title}"
                subtasks.append({
                    "index": idx,
                    "title": title,
                    "description": (
                        f"### 需求背景\n来自宏观需求拆解：\n> {cleaned}\n\n"
                        f"### 本任务目标\n{part}\n\n"
                        f"### 验收标准 (Acceptance Criteria)\n"
                        f"- [ ] 编写针对该功能的验收测试用例\n"
                        f"- [ ] 通过红灯验真与 AST 变异测试 (击杀率 ≥ 85%)\n"
                        f"- [ ] 独立沙箱 Worktree 编码施工并通过全量自动化测试\n"
                        f"- [ ] AST 语义审计无破坏性公共签名变更\n"
                    ),
                    "labels": labels,
                })
        else:
            title = cleaned if len(cleaned) <= 50 else f"{cleaned[:47]}..."
            if not any(title.startswith(pfx) for pfx in ("feat", "fix", "refactor", "chore", "test", "docs")):
                title = f"feat: {title}"
            subtasks.append({
                "index": 1,
                "title": title,
                "description": (
                    f"### 任务目标\n{cleaned}\n\n"
                    f"### 验收标准 (Acceptance Criteria)\n"
                    f"- [ ] 编写针对该功能的验收测试用例\n"
                    f"- [ ] 通过红灯验真与 AST 变异测试 (击杀率 ≥ 85%)\n"
                    f"- [ ] 独立沙箱 Worktree 编码施工并通过全量自动化测试\n"
                    f"- [ ] AST 语义审计无破坏性公共签名变更\n"
                ),
                "labels": labels,
            })

    return subtasks


class FlowService:
    """Encapsulates MCP tool business logic so it can be cleanly instantiated and unit-tested."""

    def __init__(
        self,
        repo_root: Path,
        store: StateStore,
        gl: BaseGitForge | None = None,
        forge: BaseGitForge | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.store = store
        self.gl: BaseGitForge = forge or gl or get_git_forge(self.repo_root)
        self.forge: BaseGitForge = self.gl
        self.bg_tasks: dict[str, asyncio.Task[None]] = {}
        # Mark any in-flight runs left over from a prior server restart as ESCALATED.
        self.recovered_runs = self.store.recover_orphaned_runs()

    def _register_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        self.bg_tasks[run_id] = task

        def _cleanup(_t: asyncio.Task[None]) -> None:
            if self.bg_tasks.get(run_id) is _t:
                self.bg_tasks.pop(run_id, None)

        task.add_done_callback(_cleanup)

    async def flow_init(self, requirement: str, issue_iid: int | None = None) -> dict[str, Any]:
        # Preflight checks
        if not (self.repo_root / ".git").exists():
            return {
                "error": f"初始化失败：目录 `{self.repo_root}` 不是合法的 Git 仓库（未找到 .git 目录）。请在业务代码仓库根目录下运行。"
            }
        if not shutil.which("git"):
            return {
                "error": "初始化失败：系统中未检测到 git 命令，请先安装 Git 并配置到系统 PATH 中。"
            }

        run_id = uuid.uuid4().hex[:12]
        issue_info: dict[str, Any] | None = None
        forge_warning: str | None = None
        if issue_iid is not None:
            if self.gl.enabled:
                try:
                    issue_info = await asyncio.to_thread(self.gl.get_issue, issue_iid)
                    init_comment = (
                        f"🚀 **ADK Dev Flow 已为该 Issue 启动研发流水线**\n\n"
                        f"- **Run ID**: `{run_id}`\n"
                        f"- **阶段**: `SPEC_DRAFTING`\n"
                        f"- **模式**: 8 步确定性闭环开发 (含 AST 变异测试门禁与沙箱隔离)"
                    )
                    await asyncio.to_thread(self.gl.post_issue_comment, issue_iid, init_comment)
                except Exception as exc:
                    forge_warning = f"拉取/更新 {self.gl.platform_name.upper()} Issue #{issue_iid} 失败，已继续初始化: {exc}"
            else:
                forge_warning = (
                    f"传入了 Issue #{issue_iid}，但未检测到有效启用的 Git 托管平台配置（未配置 GITLAB_URL / GITLAB_TOKEN / GITLAB_PROJECT_ID 或 GITHUB_TOKEN）。"
                    f"已跳过自动拉取 Issue 详情，请手动结合需求起草 Spec。"
                )

        self.store.create_run(run_id=run_id, issue_iid=issue_iid)
        result: dict[str, Any] = {
            "run_id": run_id,
            "status": "SPEC_DRAFTING",
            "requirement": requirement,
            "issue": issue_info,
            "gitlab_issue": issue_info,
            "repo_root": str(self.repo_root),
            "state_db_path": str(self.store.db_path),
            "next_step": (
                "请结合仓库结构起草 SpecContract（含完整可运行的 pytest 验收测试源码），"
                "向用户展示并获得明确确认后再调用 flow_lock_spec 或 flow_lock_spec_from_file。"
            ),
        }
        if forge_warning:
            result["forge_warning"] = forge_warning
            result["gitlab_warning"] = forge_warning
        return result

    def flow_lock_spec(self, run_id: str, spec: SpecContract) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}

        # 1. Mutation Verification Gate: Objective verification of test effectiveness
        mutation_dict: dict[str, Any] | None = None
        if getattr(spec, "require_mutation_gate", True):
            if spec.mutation_report and spec.mutation_report.get("is_acceptable"):
                mutation_dict = spec.mutation_report
            else:
                from adk_dev_flow.mutation import evaluate_test_against_mutants_sync

                target_sources: list[tuple[str, str]] = []
                for task in spec.tasks:
                    for tf in task.target_files:
                        full_p = (self.repo_root / tf).resolve()
                        if full_p.is_file() and full_p.suffix == ".py":
                            try:
                                src = full_p.read_text(encoding="utf-8")
                                target_sources.append((tf, src))
                            except Exception:
                                pass
                if target_sources:
                    target_rel, target_code = target_sources[0]
                    m_report = evaluate_test_against_mutants_sync(
                        target_source=target_code,
                        test_source=spec.acceptance_test_code,
                        target_module_name=target_rel,
                        min_kill_ratio=0.85,
                    )
                    mutation_dict = m_report.to_dict()
                    spec.mutation_report = mutation_dict
                    if not m_report.is_acceptable:
                        return {
                            "error": (
                                f"变异测试门禁未通过 (Kill Ratio: {m_report.kill_ratio:.1%} < 85.0%)。"
                                "当前验收测试断言有效性不足，未能击杀以下语义变异体：\n"
                                + "\n".join(m_report.survived_details[:10])
                            ),
                            "mutation_report": mutation_dict,
                        }

        ok = self.store.transition_run(
            run_id,
            expected_statuses=PRE_LOCK_STATUSES,
            new_status="SPEC_LOCKED",
            spec_json=spec.model_dump_json(),
        )
        if not ok:
            return {
                "error": (
                    "当前状态不可锁定 Spec"
                    f"（需处于起草或前置审查阶段 {PRE_LOCK_STATUSES}，实际为 {run['status']}）"
                )
            }

        ast_impact: dict[str, Any] | None = None
        try:
            from adk_dev_flow.ast_graph import CodeGraph

            cg = CodeGraph(self.repo_root)
            report = cg.analyze_impact(spec.allowed_paths)
            ast_impact = {
                "impacted_production_files": report.impacted_production_files,
                "impacted_test_files": report.impacted_test_files,
                "suggested_allowed_paths": report.suggested_allowed_paths,
                "summary": report.summary,
            }
        except Exception:
            ast_impact = None

        res: dict[str, Any] = {
            "run_id": run_id,
            "status": "SPEC_LOCKED",
            "locked_test": spec.acceptance_test_path,
            "allowed_paths": spec.allowed_paths,
            "next_step": "Spec 已冻结并通过静态校验，可调用 flow_dispatch 触发 ADK 后台隔离执行与验收。",
        }
        if mutation_dict:
            res["mutation_report"] = mutation_dict
        if ast_impact:
            res["ast_impact"] = ast_impact
        return res

    def flow_analyze_impact(
        self, target_paths: list[str], include_skeletons: bool = False
    ) -> dict[str, Any]:
        from adk_dev_flow.ast_graph import CodeGraph

        cg = CodeGraph(self.repo_root)
        if include_skeletons:
            return cg.get_test_generation_context(target_paths)
        report = cg.analyze_impact(target_paths)
        return report.to_dict()

    def _record_preflight_status(self, run_id: str) -> dict[str, Any]:
        """Move a drafting run to TEST_GENERATED. Leave any later status untouched."""
        run = self.store.get_run(run_id)
        if not run:
            return {
                "status_updated": False,
                "status_error": f"未知 run_id: {run_id}",
            }
        moved = self.store.transition_run(
            run_id,
            expected_statuses=PRE_LOCK_STATUSES,
            new_status="TEST_GENERATED",
        )
        if moved:
            return {"status_updated": True, "status": "TEST_GENERATED"}
        return {
            "status_updated": False,
            "status": run["status"],
            "status_error": (
                f"预检通过，但运行处于 {run['status']}，未改写状态。"
                "TEST_GENERATED 只允许从起草阶段进入。"
            ),
        }

    def flow_preflight_test(
        self, test_path: str, test_code: str, run_id: str | None = None
    ) -> dict[str, Any]:
        """Pre-flight test runner on baseline repository to verify TDD Red validity."""
        import ast
        import subprocess
        import sys
        from adk_dev_flow.workflow import _validate_tdd_red_outcome
        from adk_dev_flow.worktree_utils import resolve_tool_command

        # 1. Syntax check
        try:
            ast.parse(test_code, filename=test_path)
        except SyntaxError as exc:
            return {
                "ok": False,
                "exit_code": 1,
                "is_pure_semantic_failure": False,
                "error": f"测试代码存在 Python 语法错误 (line {exc.lineno}): {exc.msg}",
            }

        target_file = (self.repo_root / test_path).resolve()
        target_file.parent.mkdir(parents=True, exist_ok=True)
        old_content: bytes | None = None
        if target_file.is_file():
            try:
                old_content = target_file.read_bytes()
            except Exception:
                old_content = None

        target_file.write_text(test_code, encoding="utf-8")
        try:
            cmd = resolve_tool_command(
                "pytest",
                ["-q", str(target_file)],
                worktree=self.repo_root,
                repo_root=self.repo_root,
                env_override=os.environ.get("ADK_TDD_RED_CMD") or os.environ.get("ADK_TEST_CMD"),
                placeholder_vars={"test_path": test_path},
            )
            if cmd is None:
                cmd = [sys.executable, "-m", "pytest", "-q", str(target_file)]

            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            combined_out = f"{proc.stdout}\n{proc.stderr}"
            try:
                _validate_tdd_red_outcome(
                    returncode=proc.returncode,
                    combined_output=combined_out,
                    cmd=cmd,
                    test_path=test_path,
                )
                evidence = {
                    "ok": True,
                    "exit_code": proc.returncode,
                    "is_pure_semantic_failure": True,
                    "assertion_error_summary": combined_out.strip()[-1000:],
                    "command": " ".join(cmd),
                }
                if run_id:
                    evidence.update(self._record_preflight_status(run_id))
                return evidence
            except RuntimeError as err:
                return {
                    "ok": False,
                    "exit_code": proc.returncode,
                    "is_pure_semantic_failure": False,
                    "error": str(err),
                    "output": combined_out[-1000:],
                }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "exit_code": -1,
                "is_pure_semantic_failure": False,
                "error": "测试执行超时 (>60s)",
            }
        except Exception as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "is_pure_semantic_failure": False,
                "error": f"测试执行失败: {exc}",
            }
        finally:
            if old_content is not None:
                try:
                    target_file.write_bytes(old_content)
                except Exception:
                    pass
            else:
                try:
                    target_file.unlink(missing_ok=True)
                except Exception:
                    pass

    def flow_lock_spec_from_file(self, run_id: str, spec_file_path: str) -> dict[str, Any]:
        p = Path(spec_file_path)
        if not p.is_absolute():
            p = (self.repo_root / p).resolve()
        if not p.is_file():
            return {"error": f"规约文件未找到: {spec_file_path} (resolved: {p})"}
        try:
            spec = SpecContract.from_okf_markdown(p)
        except Exception as exc:
            return {"error": f"OKF 规约文件解析失败: {exc}"}
        return self.flow_lock_spec(run_id=run_id, spec=spec)

    async def flow_dispatch(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}

        # Atomic CAS transition prevents duplicate concurrent launches.
        ok = self.store.transition_run(
            run_id,
            expected_statuses=("SPEC_LOCKED",),
            new_status="RUNNING",
        )
        if not ok:
            return {
                "error": f"当前状态不可派发（需 SPEC_LOCKED，实际为 {run['status']}）"
            }

        task = asyncio.create_task(
            execute_workflow_run(self.repo_root, self.store, self.gl, run_id)
        )
        self._register_task(run_id, task)
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "message": "ADK 确定性流水线已在后台隔离 Worktree 启动，请调用 flow_status 查询进度。",
        }

    async def flow_resume(self, run_id: str, instruction: str) -> dict[str, Any]:
        hint = instruction.strip()
        if not hint:
            return {"error": "请提供非空的补充指令 (instruction) 后再恢复执行。"}

        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}
        if not run.get("spec_json"):
            return {"error": f"run_id={run_id} 尚未锁定 Spec，无法恢复执行。"}

        ok = self.store.transition_run(
            run_id,
            expected_statuses=("ESCALATED",),
            new_status="RUNNING",
            attempt=0,
            last_error_sig="",
            human_hint=hint,
        )
        if not ok:
            return {
                "error": f"当前状态不可恢复（仅 ESCALATED 可调用 flow_resume，实际为 {run['status']}）"
            }

        task = asyncio.create_task(
            execute_workflow_run(
                self.repo_root, self.store, self.gl, run_id, human_hint=hint
            )
        )
        self._register_task(run_id, task)
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "message": "已携人工补充指令在原隔离 Worktree 恢复流水线，请调用 flow_status 查询进度。",
        }

    def flow_status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}
        marker = self.store.read_active_marker()
        active_run_id = (marker.get("run_id") or marker.get("active_run_id")) if marker else None
        active_ids = marker.get("active_run_ids", []) if marker else []
        is_holding_lock = bool(marker and (active_run_id == run_id or run_id in active_ids))
        res = {
            "run_id": run["run_id"],
            "status": run["status"],
            "issue_iid": run["issue_iid"],
            "branch": run["branch"],
            "worktree_path": run["worktree_path"],
            "base_sha": run.get("base_sha"),
            "attempt": run["attempt"],
            "mr_iid": run["mr_iid"],
            "mr_url": run["mr_url"],
            "review": json.loads(run["review_json"]) if run["review_json"] else None,
            "human_hint": run.get("human_hint"),
            "last_error_log": (run["last_error_log"] or "")[-1500:],
            "is_holding_lock": is_holding_lock,
            "current_locked_run_id": active_run_id,
            "updated_at": run["updated_at"],
        }
        if run["status"] == "ESCALATED":
            res["guidance"] = (
                "流水线已熔断 (ESCALATED)。可通过 flow_resume 注入补充指令恢复，"
                "或调用 flow_unlock / flow_abort 退出并解除主工作区锁定。"
            )
        return res

    def flow_unlock(self, run_id: str | None = None, force: bool = False) -> dict[str, Any]:
        """安全解除主工作区的只读锁定。清除 .adk/active_run.json 并同步运行状态。"""
        marker = self.store.read_active_marker()
        marker_run_id = (marker.get("run_id") or marker.get("active_run_id")) if marker else None
        target_id = run_id or marker_run_id

        if target_id:
            run = self.store.get_run(target_id)
            if run and run["status"] in (
                "SPEC_DRAFTING",
                "SPEC_LOCKED",
                "RUNNING",
                "VERIFYING",
                "REPAIRING",
                "REVIEWING",
            ):
                self.store.update_run(
                    target_id,
                    status="ESCALATED",
                    last_error_log="\n[系统解锁] 用户调用 flow_unlock 解除工作区只读锁定。",
                )

        self.store.clear_active_run()
        return {
            "unlocked": True,
            "cleared_marker_for": target_id,
            "message": "已成功清除主工作区锁定标记 (.adk/active_run.json)，工作区已恢复可写状态。",
        }

    def flow_list(self, limit: int = 10) -> dict[str, Any]:
        runs = self.store.list_runs(limit=limit)
        items = [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "issue_iid": r["issue_iid"],
                "branch": r["branch"],
                "attempt": r["attempt"],
                "mr_url": r["mr_url"],
                "updated_at": r["updated_at"],
            }
            for r in runs
        ]
        return {"runs": items, "count": len(items)}

    async def _remove_worktree(self, worktree_path: str) -> str | None:
        wt_p = Path(worktree_path)
        res = await run_subprocess_safe(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=self.repo_root,
            timeout_sec=30.0,
        )
        if wt_p.exists():
            try:
                shutil.rmtree(wt_p, ignore_errors=True)
            except Exception:
                pass
        await run_subprocess_safe(
            ["git", "worktree", "prune"],
            cwd=self.repo_root,
            timeout_sec=15.0,
        )
        if wt_p.exists():
            return f"未能完全删除目录: {worktree_path}"
        return None

    async def flow_finalize(self, run_id: str, remove_worktree: bool = True) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}
        if run["status"] != "MR_READY":
            return {
                "error": f"当前状态不可 Finalize（需 MR_READY，实际为 {run['status']}）"
            }

        result: dict[str, Any] = {"run_id": run_id, "status": "MR_READY"}
        if run.get("mr_iid") and self.gl.enabled:
            try:
                result["mr"] = await asyncio.to_thread(self.gl.mark_mr_ready, run["mr_iid"])
            except Exception as exc:
                return {"error": f"移除 {self.gl.platform_name.upper()} PR/MR Draft 标记失败: {exc}"}

        if remove_worktree and run.get("worktree_path"):
            wt_err = await self._remove_worktree(run["worktree_path"])
            result["worktree_removed"] = wt_err is None
            if wt_err:
                result["worktree_warning"] = wt_err

        self.store._sync_active_marker()
        return result

    async def flow_abort(self, run_id: str, remove_worktree: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            return {"error": f"未知 run_id: {run_id}"}

        task = self.bg_tasks.pop(run_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        wt_warning: str | None = None
        if remove_worktree and run.get("worktree_path"):
            wt_warning = await self._remove_worktree(run["worktree_path"])

        self.store.update_run(run_id, status="ABORTED")
        result: dict[str, Any] = {"run_id": run_id, "status": "ABORTED"}
        if wt_warning:
            result["worktree_warning"] = wt_warning
        return result

    def flow_active_runs(self) -> dict[str, Any]:
        runs = self.store.get_active_runs()
        items = [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "issue_iid": r["issue_iid"],
                "branch": r["branch"],
                "worktree_path": r["worktree_path"],
                "attempt": r["attempt"],
                "updated_at": r["updated_at"],
            }
            for r in runs
        ]
        return {"active_runs": items, "count": len(items)}

    def flow_dashboard(self, run_id: str | None = None) -> dict[str, Any]:
        """获取或生成用于像素工坊动画看板的运行态数据。"""
        status_info: dict[str, Any]
        if run_id:
            status_info = self.flow_status(run_id)
        else:
            active = self.store.get_active_runs()
            if active:
                status_info = self.flow_status(active[0]["run_id"])
            else:
                recent = self.store.list_runs(limit=1)
                status_info = (
                    self.flow_status(recent[0]["run_id"])
                    if recent
                    else {"status": "IDLE", "message": "暂无活跃任务"}
                )

        return {
            "content": (
                f"ADK 像素工坊看板已就绪。当前状态: {status_info.get('status', 'IDLE')}，"
                f"轮次: {status_info.get('attempt', 0)}"
            ),
            "structuredContent": status_info,
        }

    async def flow_abort_all(self, remove_worktrees: bool = False) -> dict[str, Any]:
        active_runs = self.store.get_active_runs()
        aborted = []
        for r in active_runs:
            res = await self.flow_abort(r["run_id"], remove_worktree=remove_worktrees)
            aborted.append(res)
        return {"aborted_runs": aborted, "count": len(aborted)}

    async def flow_create_issue(
        self,
        title: str,
        description: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """在 GitLab / GitHub（或本地离线模式）创建新 Issue。"""
        clean_title = title.strip()
        if not clean_title:
            return {"ok": False, "error": "Issue 标题不能为空"}

        try:
            res = await asyncio.to_thread(
                self.gl.create_issue,
                title=clean_title,
                description=description.strip(),
                labels=labels or [],
            )
            return {
                "ok": True,
                "platform": self.gl.platform_name,
                "issue_iid": res["iid"],
                "title": res["title"],
                "description": res.get("description", ""),
                "labels": res.get("labels", []),
                "web_url": res.get("web_url", ""),
                "next_step": f"可立即调用 flow_init(requirement='{clean_title}', issue_iid={res['iid']}) 启动确定性研发流程。",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"在 {self.gl.platform_name} 创建 Issue 失败: {exc}",
            }

    async def flow_plan_issues(
        self,
        epic_requirement: str,
        auto_create: bool = False,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """将宏观需求拆解为原子 Issue 清单，支持预览或一键自动建单。"""
        planned = parse_epic_into_subtasks(epic_requirement, default_labels=labels)
        if not planned:
            return {"ok": False, "error": "未能从需求文本中提取有效任务，请提供详细需求内容"}

        created_issues: list[dict[str, Any]] = []
        if auto_create:
            for task in planned:
                try:
                    c = await asyncio.to_thread(
                        self.gl.create_issue,
                        title=task["title"],
                        description=task["description"],
                        labels=task.get("labels", []),
                    )
                    created_issues.append({
                        "issue_iid": c["iid"],
                        "title": c["title"],
                        "web_url": c.get("web_url", ""),
                        "labels": c.get("labels", []),
                    })
                except Exception as exc:
                    created_issues.append({
                        "error": str(exc),
                        "title": task["title"],
                    })

        return {
            "ok": True,
            "auto_created": auto_create,
            "platform": self.gl.platform_name,
            "epic_requirement": epic_requirement,
            "planned_issues": planned,
            "created_issues": created_issues if auto_create else [],
            "next_step": (
                f"已自动创建 {len(created_issues)} 个 Issue，请按顺序或并发调用 flow_init 逐一开展研发。"
                if auto_create
                else f"已规划 {len(planned)} 个原子任务。确认无误后可调用 flow_plan_issues(..., auto_create=True) 或通过 flow_create_issue 逐项创建。"
            ),
        }


def resolve_repo_root(custom_root: Path | None = None) -> Path:
    """Resolve the business repository root directory.

    Priority: custom_root -> REPO_ROOT -> WORKSPACE_FOLDER -> CODEBUDDY_WORKSPACE_ROOT -> os.getcwd().
    If candidate lacks .git, searches upwards for the nearest Git repository root.
    """
    if custom_root is not None:
        return custom_root.resolve()

    candidate = (
        os.environ.get("REPO_ROOT")
        or os.environ.get("WORKSPACE_FOLDER")
        or os.environ.get("CODEBUDDY_WORKSPACE_ROOT")
        or ""
    ).strip()
    if candidate:
        p = Path(candidate)
        return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()

    cwd = Path.cwd().resolve()
    curr = cwd
    while curr != curr.parent:
        if (curr / ".git").is_dir():
            return curr
        curr = curr.parent
    return cwd


def load_project_config(repo_root: Path, apply_to_env: bool = True) -> dict[str, str]:
    """Load project-local configuration from .adk/config.json, .adk/config.local.json, and .env.

    Enables multiple projects to configure different GitLab projects, models, and test commands
    without modifying the global plugin bundle or polluting global environment variables.
    """
    root = repo_root.resolve()
    merged: dict[str, str] = {}

    KEY_MAP = {
        "gitlab_url": "GITLAB_URL",
        "gitlab_token": "GITLAB_TOKEN",
        "gitlab_project_id": "GITLAB_PROJECT_ID",
        "gitlab_ca_bundle": "GITLAB_CA_BUNDLE",
        "gitlab_ssl_verify": "GITLAB_SSL_VERIFY",
        "github_token": "GITHUB_TOKEN",
        "github_repository": "GITHUB_REPOSITORY",
        "github_api_url": "GITHUB_API_URL",
        "test_cmd": "ADK_TEST_CMD",
        "tdd_red_cmd": "ADK_TDD_RED_CMD",
        "lint_cmd": "ADK_LINT_CMD",
        "regression_cmd": "ADK_REGRESSION_CMD",
        "worker_model": "ADK_WORKER_MODEL",
        "critic_model": "ADK_CRITIC_MODEL",
        "worker_provider": "ADK_WORKER_PROVIDER",
        "codebuddy_worker_extra_args": "CODEBUDDY_WORKER_EXTRA_ARGS",
    }

    # 1. Read .env if present
    env_file = root / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    merged[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass

    # 2. Read .adk/config.json and .adk/config.local.json
    for cfg_name in ("config.json", "config.local.json"):
        cfg_path = root / ".adk" / cfg_name
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    env_sub = data.get("env")
                    if isinstance(env_sub, dict):
                        for k, v in env_sub.items():
                            merged[str(k)] = str(v)
                    for k, v in data.items():
                        if k == "env":
                            continue
                        mapped_k = KEY_MAP.get(k.lower())
                        if mapped_k:
                            val_str = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                            merged[mapped_k] = val_str
                        elif k.isupper():
                            val_str = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                            merged[k] = val_str
            except Exception:
                pass

    # 3. Read .adk/env.json (plain uppercase KEY=value map). This is written by
    #    the deterministic-dev workflow; it shares the same namespace as the
    #    config keys above and is the most specific source for a project.
    env_json = root / ".adk" / "env.json"
    if env_json.is_file():
        try:
            data = json.loads(env_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        merged[k] = v
        except Exception:
            pass

    if apply_to_env:
        for k, v in merged.items():
            os.environ[k] = v

    return merged


def create_mcp_server(
    repo_root: Path | None = None,
    store: StateStore | None = None,
    gl: BaseGitForge | None = None,
    forge: BaseGitForge | None = None,
) -> tuple[FastMCP, FlowService]:
    resolved_root = resolve_repo_root(repo_root)
    load_project_config(resolved_root, apply_to_env=True)
    resolved_store = store or StateStore(resolved_root)
    resolved_forge = forge or gl or get_git_forge(resolved_root)
    service = FlowService(resolved_root, resolved_store, resolved_forge)

    mcp = FastMCP("adk-dev-flow")

    @mcp.tool()
    async def flow_create_issue(
        title: str,
        description: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """在 GitLab / GitHub（或本地离线模式）创建新 Issue，作为确定性开发闭环的前置追踪工单。"""
        return await service.flow_create_issue(title=title, description=description, labels=labels)

    @mcp.tool()
    async def flow_plan_issues(
        epic_requirement: str,
        auto_create: bool = False,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """将宏观需求/PRD 拆解为原子 Issue 清单（支持预览或 auto_create=True 一键批量在平台建单）。"""
        return await service.flow_plan_issues(
            epic_requirement=epic_requirement,
            auto_create=auto_create,
            labels=labels,
        )

    @mcp.tool()
    async def flow_init(requirement: str, issue_iid: int | None = None) -> dict[str, Any]:
        """初始化确定性开发流。若提供 GitLab/GitHub Issue 编号，将自动拉取详情。"""
        return await service.flow_init(requirement=requirement, issue_iid=issue_iid)

    @mcp.tool()
    def flow_lock_spec(run_id: str, spec: SpecContract) -> dict[str, Any]:
        """静态校验并冻结用户已确认的结构化 Spec 与验收测试用例。"""
        return service.flow_lock_spec(run_id=run_id, spec=spec)

    @mcp.tool()
    def flow_lock_spec_from_file(run_id: str, spec_file_path: str) -> dict[str, Any]:
        """读取 OKF (Open Knowledge Format) Markdown 规约文件并冻结锁定该运行轮次。"""
        return service.flow_lock_spec_from_file(run_id=run_id, spec_file_path=spec_file_path)

    @mcp.tool()
    def flow_analyze_impact(
        target_paths: list[str], include_skeletons: bool = False
    ) -> dict[str, Any]:
        """使用 AST 代码图谱静态分析目标代码的影响面（爆炸半径），推导关联生产代码、调用方与受影响测试用例，可包含测试骨架上下文。"""
        return service.flow_analyze_impact(
            target_paths=target_paths, include_skeletons=include_skeletons
        )

    @mcp.tool()
    def flow_preflight_test(
        test_path: str, test_code: str, run_id: str | None = None
    ) -> dict[str, Any]:
        """在当前基线代码上预检执行验收测试，客观判定是否属于纯净有效的 TDD 红灯（排除空测试通过与语法/Fixture假红灯）。"""
        return service.flow_preflight_test(
            test_path=test_path, test_code=test_code, run_id=run_id
        )

    @mcp.tool()
    async def flow_dispatch(run_id: str) -> dict[str, Any]:
        """在后台启动 ADK 2.x Graph Workflow（建 Worktree、隔离编码、审计与验收自愈、Critic 审查、建 MR）。"""
        return await service.flow_dispatch(run_id=run_id)

    @mcp.tool()
    async def flow_resume(run_id: str, instruction: str) -> dict[str, Any]:
        """对已熔断（ESCALATED）的流水线注入人工补充指令，并在原隔离 Worktree 中恢复执行。"""
        return await service.flow_resume(run_id=run_id, instruction=instruction)

    @mcp.tool()
    def flow_status(run_id: str) -> dict[str, Any]:
        """查询确定性开发流的实时阶段、修复轮次、报错日志及 MR 链接。"""
        return service.flow_status(run_id=run_id)

    @mcp.tool()
    def flow_list(limit: int = 10) -> dict[str, Any]:
        """列出最近的受控开发流记录（用于跨会话找回 run_id）。"""
        return service.flow_list(limit=limit)

    @mcp.tool()
    async def flow_finalize(run_id: str, remove_worktree: bool = True) -> dict[str, Any]:
        """确认无误后移除 Draft 标记，将内网 GitLab MR 置为 Ready 并可选清理本地隔离 Worktree。"""
        return await service.flow_finalize(run_id=run_id, remove_worktree=remove_worktree)

    @mcp.tool()
    async def flow_abort(run_id: str, remove_worktree: bool = False) -> dict[str, Any]:
        """中止当前受控开发流、终止后台子进程并解除主工作区防呆锁。"""
        return await service.flow_abort(run_id=run_id, remove_worktree=remove_worktree)

    @mcp.tool()
    def flow_unlock(run_id: str | None = None, force: bool = False) -> dict[str, Any]:
        """安全解除主工作区的只读锁定，清除 active_run.json 锁标记。"""
        return service.flow_unlock(run_id=run_id, force=force)

    @mcp.tool()
    def flow_active_runs() -> dict[str, Any]:
        """查询当前正在并发进行的所有活跃开发流（支持多任务状态监控）。"""
        return service.flow_active_runs()

    @mcp.tool()
    async def flow_abort_all(remove_worktrees: bool = False) -> dict[str, Any]:
        """一键终止当前所有进行中的受控开发流并释放所有锁与资源。"""
        return await service.flow_abort_all(remove_worktrees=remove_worktrees)

    APP_URI = "ui://adk-dev-flow/dashboard.html"

    @mcp.resource(
        APP_URI,
        name="ADK Flow Cartoon Dashboard",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"prefersBorder": False}},
    )
    def get_dashboard_resource() -> str:
        """提供用于 Antigravity 侧边栏/内联渲染的像素工坊看板 HTML 资源。"""
        return get_dashboard_html()

    PANE_META = {
        "ui": {"resourceUri": APP_URI},
        "com.google.antigravity/ui": {
            "entrypoints": [
                {"type": "aux_pane", "title": "ADK 像素工坊看板"},
                {"type": "global", "title": "ADK 像素工坊看板"},
            ]
        },
    }

    @mcp.tool(
        name="flow_dashboard",
        title="打开 ADK 像素工坊动画看板",
        meta=PANE_META,
    )
    def flow_dashboard(run_id: str | None = None) -> dict[str, Any]:
        """打开或刷新 ADK 确定性开发流像素卡通工人工位动画看板。"""
        return service.flow_dashboard(run_id=run_id)

    return mcp, service


def main() -> None:
    mcp, _ = create_mcp_server()
    mcp.run()


if __name__ == "__main__":
    main()
