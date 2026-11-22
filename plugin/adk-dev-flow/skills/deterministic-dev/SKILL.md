---
name: deterministic-dev
description: 当用户说“用标准流程/确定性流程开发”、“按流程处理 Issue”或进行受控 Python 功能开发与缺陷修复时自动激活。
---

# 确定性研发流协议（指挥台模式）

当此 Skill 激活时，你充当**研发指挥台**，严禁直接编辑仓库源码，必须按以下状态机调用 `adk-dev-flow` MCP 工具：

1. **初始化或找回上下文**：
   - 新建任务：立即调用 `flow_init(requirement=..., issue_iid=...)`（自动自适应 GitLab / GitHub 平台并拉取 Issue）。
   - 若跨会话找回任务或存在多任务并发：调用 `flow_active_runs()` 查看所有活跃流水线，或调用 `flow_list()` 检索历史记录。
2. **起草 Spec 契约与锁定验收测试（强制人工门禁）**：
   - 只读浏览相关 Python 模块与 `.adk/knowledge/rules/` 下的工程规约。
   - **AST 影响面推导**：建议先调用 `flow_analyze_impact(target_paths=[...])` 自动推导目标文件的爆炸半径、关联生产文件与受影响测试集，智能推荐最小安全白名单。
   - 起草符合 `SpecContract` 结构或写入 `.adk/specs/issue-{iid}.md`（OKF Markdown 格式）。
   - 包含：改动范围白名单 `allowed_paths`、原子任务拆解 `tasks`（目标文件必须在白名单内）、增量依赖 `extra_dependencies`、以及一份**完整可运行且语法正确的 `pytest` 验收测试代码**。
   - **TDD Red 校验**：验收测试必须包含有效断言，确保在编写业务代码前在基线分支上执行失败（`require_tdd_red: true`）。
   - 向用户清晰展示方案，等待用户明确确认。**未获确认前，严禁调用锁定工具。**
3. **冻结规约并派发流水线**：
   - 用户确认后，可通过以下两种方式之一完成冻结：
     - **OKF 文件驱动**：调用 `flow_lock_spec_from_file(run_id=..., spec_file_path=".adk/specs/...")`。
     - **结构化参数驱动**：调用 `flow_lock_spec(run_id=..., spec=...)`。
   - 锁定成功后调用 `flow_dispatch(run_id=...)`，系统将拉起后台隔离 Worktree 开始工兵编码、自动自愈与 Critic 审查。
4. **追踪、实时动画看板监控与人工介入**：
   - 可调用 `flow_status(run_id=...)` 汇报当前阶段、重试轮次、Worktree 状态与错误摘要。
   - **像素工坊动态看板**：随时可调用 `flow_dashboard(run_id=...)` 在 IDE 侧边栏（Aux Pane）或对话内联打开 6 大卡通工位实时动画，或提示用户在终端运行 `adk-dev-flow-dashboard --open` 开启独立 Web 监控。
   - 若进入 `ESCALATED`（熔断）：向用户反馈失败根因，请用户选择：
     - **补充指令继续自愈**：调用 `flow_resume(run_id=..., instruction=...)` 在原 Worktree 中恢复运行；
     - **终止退出**：调用 `flow_abort(run_id=..., remove_worktree=...)` 解锁并清理；
     - **全局紧急中止**：若多任务出现异常，可调用 `flow_abort_all()` 一键强杀所有活跃流水线。
   - 若进入 `MR_READY`：系统已自动将 OKF 审查报告提交至分支 `.adk/audit/review_{run_id}.md`，并生成 GitLab / GitHub PR/MR。向用户展示 PR/MR 链接与审查建议，询问是否调用 `flow_finalize(run_id=..., remove_worktree=True)` 移除 Draft 标记并清理隔离工作区。
