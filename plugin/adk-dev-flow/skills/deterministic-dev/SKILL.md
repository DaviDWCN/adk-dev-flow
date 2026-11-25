---
name: deterministic-dev
description: 当用户说“用标准流程/确定性流程开发”、“按流程处理 Issue”或进行受控 Python 功能开发与缺陷修复时自动激活。
---

# 确定性研发流协议（指挥台模式）

当此 Skill 激活时，你充当**研发指挥台**，严禁直接编辑仓库源码，必须按以下状态机调用 `adk-dev-flow` MCP 工具：

0. **Issue 驱动与宏观需求拆解（Issue-Driven Development 前置）**：
   - 当面对复杂宏观需求（Epic / PRD）或用户要求“先建 Issue 再开发”时：
     - 调用 `flow_plan_issues(epic_requirement=..., auto_create=False)` 智能解析需求为原子任务清单（含任务背景与验收标准）并向用户确认；
     - 确认后调用 `flow_plan_issues(..., auto_create=True)` 或 `flow_create_issue(title=..., description=...)` 批量在 GitLab / GitHub 上建单；
     - 针对每个 Issue 独立调用 `flow_init(requirement=..., issue_iid=...)` 接入确定性开发流水线。
1. **初始化或找回上下文**：
   - 新建任务：立即调用 `flow_init(requirement=..., issue_iid=...)`（自动自适应 GitLab / GitHub 平台并拉取 Issue，流水线将自动在 Issue 下追加启动通知，并自动安装 L2 pre-commit 登记门禁）。
   - **流程外工作收编（flow_adopt 轻量通道）**：若 Issue 的工作已经在分支上直接进行（未走流水线），调用 `flow_adopt(issue_iid=..., branch=..., note=...)` 补登记为 `ADOPTED` run —— 大屏立即纳入监控；工作完成提交后调用 `flow_adopt_finalize(run_id=...)` 记录分支尖端提交为证据并置为 MR_READY。**收编是登记不是验证**（大屏会以琥珀色徽章如实标注 post_hoc）；适合 doc-only / ADR / 配置类轻任务，代码类任务建议收编后仍走锁定与验收流程。
   - **commit 门禁（确定性研发约束）**：在宣告 issue 的分支（`NNN-*` / `adk/issue-N-*`）上 commit 时，系统校验该 issue 存在有效 run（ABORTED 不计）；未登记会被 PreToolUse hook / pre-commit 拦截并提示收编路径。此约束只拦 commit，不拦编辑与探索。紧急豁免 `ADK_FLOW_ENFORCE=off` 会留痕到 `.adk/exemptions.log` 并上屏。
   - 若跨会话找回任务或存在多任务并发：调用 `flow_active_runs()` 查看所有活跃流水线，或调用 `flow_list()` 检索历史记录。
   - 若因异常中断导致主工作区处于只读锁定：调用 `flow_unlock()` 安全解除锁定。
2. **架构设计与 AST 影响面推导（设计阶段）**：
   - 调用 `flow_analyze_impact(target_paths=[...], include_skeletons=True)` 自动推导目标文件的爆炸半径、关联生产文件、受影响测试集以及上游调用点（callers），提取目标文件类型骨架。
   - 梳理详细架构设计、接口契约变更（函数/Pydantic/类签名）、原子任务分解 `tasks` 与最小安全白名单 `allowed_paths`。
3. **测试合成与基线真实红灯预检（测试阶段）**：
   - 依据设计契约与 AST 影响面骨架，独立编写严密的 Pytest 验收测试用例。
   - **基线客观红灯验证**：调用 `flow_preflight_test(test_path=..., test_code=..., run_id=...)` 在基线分支上预先执行该用例。
   - 系统将客观证明该测试以纯净的业务断言（AssertionError）失败，坚决拦截空断言假测试（exit=0）与语法/Fixture假红灯。
4. **前置双审与强制人工门禁（Critic & HITL Gate）**：
   - 审查设计方案与测试用例的契约一致性，确保业务需求完全被有效断言覆盖且未越过 AST 边界。
   - 向用户清晰展示方案全貌：**📐 设计方案 + 🧪 验收测试 + 🔴 真实红灯报错摘要 + 🧐 前置审查报告**。
   - **未获确认前，严禁调用锁定工具。** 用户明确确认后调用 `flow_lock_spec` 或 `flow_lock_spec_from_file` 冻结规约与防篡改签名。
5. **派发后台隔离沙箱施工（工兵 Agent）**：
   - 锁定成功后调用 `flow_dispatch(run_id=...)`，系统拉起后台独立 Git Worktree。
   - 测试文件被物理 Hash 锁定（只读隔离区，严禁工兵修改测试），工兵仅在 `allowed_paths` 白名单内受控编码。
   - **发现型 / 新建模块 spike**：在 Spec 中标记 `spike=true`（工兵探索预算大幅提高：max_turns ≥60、超时翻倍，并建议搭配更强模型）；若外部库逆向工作量远超工兵能力，应跳过 `flow_dispatch`，由主会话在 Worktree 中亲自实现后再走验收流程。每轮修复的工兵自述与实际改动文件会回喂下一轮（上一轮零改动会被明确标记）。
6. **确定性机器硬门禁与定向自愈循环**：
   - 自动执行三重机器硬检验：Worktree 物理审计（防篡改与越权） ➔ Ruff 规范扫描 ➔ Pytest 验收测试。
   - 若校验失败，提取报错指纹进入定向自愈（最多 3 轮）；若连续两轮出现相同报错指纹则自动触发防死循环熔断。
7. **确定性 Step 8 门禁与交付保护（只读审查 & PR 缓冲）**：
   - **AST 静态硬门禁一票否决**：若检测到破坏向后兼容的公共签名变更或危险动态代码（eval/exec），系统硬编码阻断批准，大模型无权推翻。
   - **Critic 只读顾问**：独立 Critic AI 仅以只读权限审查 Git Diff 并生成 OKF Review 审计文档，绝对禁止擅自篡改已验证代码。
   - **PR/MR 安全交付**：代码以 PR/MR 缓冲带交付，严禁无人工核准直推生产主干。
8. **追踪、实时动画看板监控与人工介入**：
   - 可调用 `flow_status(run_id=...)` 汇报当前阶段、重试轮次、Worktree 状态与错误摘要。
   - **像素工坊动态看板**：随时可调用 `flow_dashboard(run_id=...)` 在 IDE 侧边栏（Aux Pane）或对话内联打开 6 大卡通工位实时动画，或提示用户在终端运行 `adk-dev-flow-dashboard --open` 开启独立 Web 监控。
   - 若进入 `ESCALATED`（熔断）：向用户反馈失败根因，请用户选择：
     - **补充指令继续自愈**：调用 `flow_resume(run_id=..., instruction=...)` 在原 Worktree 中恢复运行；
     - **终止退出**：调用 `flow_abort(run_id=..., remove_worktree=...)` 解锁并清理；
     - **安全解锁工作区**：若需要强制复位工作区，可调用 `flow_unlock(force=True)`；
     - **全局紧急中止**：若多任务出现异常，可调用 `flow_abort_all()` 一键强杀所有活跃流水线。
   - 若进入 `MR_READY`：系统已自动将 OKF 审查报告提交至分支 `.adk/audit/review_{run_id}.md`，并生成 GitLab / GitHub PR/MR。向用户展示 PR/MR 链接与审查建议，询问是否调用 `flow_finalize(run_id=..., remove_worktree=True)` 移除 Draft 标记并清理隔离工作区。
