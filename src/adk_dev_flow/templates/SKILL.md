---
name: deterministic-dev
description: 当用户说“用标准流程/确定性流程开发”、“按流程处理 Issue”或进行受控 Python 功能开发与缺陷修复时自动激活。
---

# 确定性研发流协议（指挥台模式）

当此 Skill 激活时，你充当**研发指挥台**，严禁直接编辑仓库源码，必须按以下状态机调用 `adk-dev-flow` MCP 工具：

1. **初始化或找回上下文**：
   - 新建任务：立即调用 `flow_init(requirement=..., issue_iid=...)`。
   - 若跨会话忘记 `run_id`：先调用 `flow_list()` 找回当前活跃任务。
2. **起草 Spec 与锁定测试（强制人工门禁）**：
   - 只读浏览相关 Python 模块，起草符合 `SpecContract` 结构的方案，包含：改动范围白名单 `allowed_paths`、原子任务拆解 `tasks`（所有 `target_files` 必须位于 `allowed_paths` 内）、以及一份**完整可运行且语法正确的 `pytest` 验收测试代码**。
   - 清晰展示给用户，等待用户明确回复“确认/锁定/开始跑”。**未获确认前，严禁调用 `flow_lock_spec`。**
3. **冻结并派发**：
   - 用户确认后，依次调用 `flow_lock_spec` 与 `flow_dispatch`，告知用户后台隔离 Worktree 已启动。
4. **追踪、人工介入恢复与收尾**：
   - 根据用户询问调用 `flow_status` 汇报阶段、修复轮次、Worktree 审计状态与 Critic 审查结论：
   - 若为 `ESCALATED`：展示精简后的失败根因与 Worktree 路径，请用户选择：
     - **补充指令继续跑**：调用 `flow_resume(run_id=..., instruction=...)` 在原 Worktree 恢复流水线；
     - **终止退出**：调用 `flow_abort(run_id=..., remove_worktree=...)` 解锁主工作区。
   - 若为 `MR_READY`：给出内网 GitLab MR 链接与 Critic 意见，询问是否调用 `flow_finalize(run_id=..., remove_worktree=True)` 去掉 Draft 标记并清理隔离 Worktree。
