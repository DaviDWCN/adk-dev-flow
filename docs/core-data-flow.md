---
type: ArchitectureDoc
title: '核心数据流与状态边界全景说明'
status: active
component: core-pipeline
date: '2026-11-28'
---

# 核心数据流与状态边界全景说明

本文档基于源码逐跳梳理 `adk-dev-flow` 的核心数据流（截至 `2026-11-28` 实测实现），覆盖状态真源、前置契约与门禁、Worktree 沙箱、验证自愈闭环、交付与知识回流、以及看板/Commit 门禁遥测。本文仅陈述当前源码中实际存在且经过核对的读写路径；已发现的边界与缺口在文末集中列出。

---

## 1. 端到端核心数据流拓扑

```mermaid
flowchart TD
    User["指挥台 / 用户输入"] -->|"flow_init / flow_adopt"| DB[(".adk/workflow.db<br/>runs + attempt_logs")]
    DB -->|"状态同步 _sync_active_marker"| Marker[".adk/active_run.json"]
    Marker -->|"Edit/Write 拦截"| Guard["guard.py<br/>(adk-workflow-guard)"]
    DB -->|"Branch<->Run 检查"| Enforce["enforcement.py / enforce_flow.py<br/>(pre-commit / PreToolUse Bash)"]

    User -->|"flow_analyze_impact"| CPG["CodeGraph (.adk/cache/cpg_cache.db)"]
    User -->|"flow_preflight_test"| Preflight["主仓临时写入->pytest红灯->还原"]
    User -->|"flow_lock_spec / from_file"| Lock{"SpecContract 校验 &<br/>require_tdd_red=false 时变异测试"}
    Lock -->|"CAS -> SPEC_LOCKED + spec_json"| DB

    DB -->|"flow_dispatch / flow_resume"| Prep["prepare_worktree_node<br/>建分支/Worktree + 提交测试/Spec + 可选依赖/红灯"]
    Prep -->|"CAS -> RUNNING + SHAs"| DB
    Prep -->|"node_input (worktree, SHAs, human_hint)"| Worker["execute_worker_node<br/>Prompt(Spec + AST骨架 + OKF规则 + Repair Hint)"]
    CPG -->|"Worktree内构建AST骨架"| Worker
    Rules[".adk/knowledge/rules/*.md"] -->|"按 paths/tags 路由"| Worker
    Worker -->|"ADK_WORKER_PROVIDER (CodeBuddy CLI / Mock)"| Sandbox["隔离 Worktree (.adk/worktrees/<run_id>)"]

    Sandbox --> Verify["verify_python_node<br/>1.物理Git/Hash审计 2.zero_change 3.Lint/Pytest/回归"]
    Verify -->|"失败且未超限 -> REPAIRING"| DB
    Verify -->|"retry (attempt+1, last_error_log, err_sig)"| Worker
    Verify -->|"零改动 / 同指纹连错 / 超限 -> ESCALATED"| Escalate["escalate_node"]
    Verify -->|"passed -> REVIEWING"| Publish["publish_mr_node"]

    Publish -->|"自动 git commit 工作区改动"| AST["audit_ast_diff + DFG 污点 + symbol_alignment"]
    AST -->|"可选 (ADK_ENABLE_CRITIC=1 / ADK_CRITIC_MODEL)"| Critic["run_readonly_critic_review (locked_test_commit_sha..HEAD)"]
    Critic --> HardVeto{"enforce_deterministic_verdict_guard<br/>verdict.approved ?"}
    AST --> HardVeto
    HardVeto -->|"写入本地 .adk/audit/review_<run_id>.md"| AuditMD[".adk/audit/review_<run_id>.md"]
    HardVeto -->|"False -> ESCALATED (不推远端/不蒸馏)"| DB
    HardVeto -->|"True -> 可选蒸馏(attempt>0) + push + MR/PR"| Forge["GitLab / GitHub Forge & head_sha -> MR_READY"]
    Forge -->|"attempt>0 时写入"| Rules
    DB -->|"轮询 / SSE / Tool"| Dash["dashboard.py (/api/status, /api/events, flow_dashboard)"]
```

---

## 2. 核心数据真源（Sources of Truth）

| 存储/文件 | 写入方 | 读取方 | 承载核心字段与语义 |
|---|---|---|---|
| `.adk/workflow.db` (`runs`) | `StateStore` (`mcp_server.py`, `workflow.py`) | `mcp_server.py`, `dashboard.py`, `enforcement.py`, `okf.py` | 流水线唯一状态真源：`run_id`, `status`, `issue_iid`, `spec_json`, `branch`, `worktree_path`, `base_sha`, `head_sha`, `locked_test_sha256`, `locked_test_commit_sha`, `mr_iid`, `mr_url`, `attempt`, `last_error_sig`, `last_error_log`, `review_json`, `human_hint`, `evidence_type`, `updated_at`。 |
| `.adk/workflow.db` (`attempt_logs`) | `verify_python_node`（每轮校验通过或失败时写入） | `execute_worker_node`（fallback 读取）, `flow_status`, `publish_mr_node`（提取真实错误用于知识蒸馏） | 逐轮物理日志：`attempt`, `err_sig`, `combined_err`, `worker_summary`, `changed_files`, `created_at`。 |
| `.adk/active_run.json` | `StateStore._sync_active_marker`（每次建单或 `status` 变更自动同步） | `guard.py`, `flow_status`, `flow_unlock` | 活跃锁标记：`run_id`, `status`, `active_run_ids`, `active_runs`；无活跃状态时自动删除。 |
| `.adk/specs/issue-{iid}.md` / `task-{id6}.md` | `prepare_worktree_node` 与 `publish_mr_node`（`save_and_commit_spec_in_worktree`）；`flow_adopt_finalize`（`AdoptedSpec`） | `flow_lock_spec_from_file`, `okf.py` | OKF `DevSpec` 或 `AdoptedSpec` 规约归档，提交在目标分支中。 |
| `.adk/audit/review_{run_id}.md` | `publish_mr_node` | `okf.py` | OKF `ReviewVerdict` 审查结论及 `provenance` (`base_sha`, `locked_test_commit_sha`, `attempt`)。 |
| `.adk/cache/cpg_cache.db` | `CodeGraph`（默认路径：`{repo_or_worktree}/.adk/cache/cpg_cache.db`） | `CodeGraph.sync_git` | 按 Git Blob OID 缓存的 `FileCpg` 压缩索引（详见 `docs/cpg-cas-architecture.md`）。 |
| `.adk/knowledge/rules/*.md` | 人工维护基线规则；`publish_mr_node`（仅 `approved=True` 且 `attempt>0` 时写入 `learned_{run_id[:8]}.md`） | `execute_worker_node` (`_load_okf_rules`) | OKF `EngineeringRule`，按 `status`、`paths`、`tags` 路由注入工兵上下文。 |

---

## 3. 分阶段数据转换与约束链

### 3.1 阶段 A：建单、草拟与契约锁定（Pre-Lock）

1. **`flow_init` (`mcp_server.py`)**：
   - 在 `.adk/workflow.db` 写入 `status="SPEC_DRAFTING"`、`evidence_type="pipeline"`（此时 `branch=None`），同步 `.adk/active_run.json`。
   - 尽力安装 `.git/hooks/pre-commit`（L2 登记门禁）。
2. **`flow_analyze_impact` & `flow_preflight_test` (`mcp_server.py`)**：
   - `flow_analyze_impact` 通过 `CodeGraph(root)` 计算影响面/骨架；若 `run_id` 处于 `SPEC_DRAFTING`，以 CAS 推进至 `SPEC_DESIGNED`。
   - `flow_preflight_test` 对 `test_path` 加路径锁后**临时写入主仓库**，通过 `resolve_tool_command("pytest", ...)` 执行真实红灯预检（`_validate_tdd_red_outcome`），`finally` 还原或删除测试文件；若传入 `run_id`，推进至 `TEST_GENERATED`。
   - **重要边界**：上述两个工具只推进 `runs.status`，**不会自动将 `impact_analysis` 或 `red_evidence` 写入数据库**；只有调用方在 `flow_lock_spec` 中显式放入 `SpecContract`，看板（`dashboard.py`）和归档 Markdown 才会展示这些字段。
3. **`flow_lock_spec` / `flow_lock_spec_from_file` (`mcp_server.py`, `models.py`)**：
   - `SpecContract` 静态校验：规范化路径、禁止 `..` 与受保护路径、`ast.parse` 语法检查、校验 `task.target_files` 均位于 `allowed_paths` 且不等于 `acceptance_test_path`。
   - **变异测试门禁**：仅当 `require_mutation_gate=True`、未预先传入已通过的 `mutation_report` 且 `require_tdd_red=False` 时触发 `evaluate_test_against_mutants_sync`（使用 `ADK_PYTHON` 或当前解释器），击杀率 `< 85%` 直接拒绝锁定。
   - **OKF 解析边界**：`flow_lock_spec_from_file` 调用 `SpecContract.from_okf_markdown()`，解析 YAML Frontmatter 与正文里的 `issue_iid`、`title`、`summary`、`target_branch`、`base_branch`、`branch`、`allowed_paths`、`acceptance_test_path`、`acceptance_test_code`、`require_tdd_red`、`require_mutation_gate`、`spike`、`extra_dependencies`、`tasks` 与 `design_doc`（优先读 `meta.design_doc`，否则提取正文 `## Architecture Design` 区块）；运行时证据字段（`impact_analysis`、`red_evidence`、`pre_critic_report`、`mutation_report`）仍需通过 `flow_lock_spec` 的结构化 `SpecContract` 传入。
   - CAS 推进至 `SPEC_LOCKED` 并将完整 `spec.model_dump_json()` 写入 `runs.spec_json`。

### 3.2 阶段 B：沙箱搭建与锁定基线（`prepare_worktree_node`）

1. 计算开发分支名：优先沿用 `runs.branch` 或 `spec.branch`，否则按 `ADK_BRANCH_PATTERN` 或默认 `adk/issue-{iid or 'task'}-{run_id[:6]}` 生成；worktree 路径为 `.adk/worktrees/{run_id}`。
2. 首次建 Worktree 时：
   - 基准分支取 `spec.base_branch or spec.target_branch`。仅当 `spec.base_branch` 显式指定且不存在时报错中止；未指定 `base_branch` 且 `target_branch` 建树失败时，依次尝试从 `HEAD` 创建分支或检出已存在的同名分支。
   - 记录写测试之前的 `base_sha`。
   - 写入 `acceptance_test_path` 并计算 `locked_test_sha256`，提交验收测试，再调用 `save_and_commit_spec_in_worktree` 提交 `.adk/specs/`，随后记录 **`locked_test_commit_sha`**（工兵真实变更的起点基线）。
   - 若指定 `extra_dependencies`，在 worktree/主仓虚拟环境安装；若 `require_tdd_red=True` 且 `ADK_SKIP_TDD_RED != "1"`，在基线 worktree 再执行一次 `run_tdd_red_preflight`。
3. Resume 恢复路径（Worktree 已存在）：执行 `sanitize_worktree_state` 清理残留 `index.lock`，保留现有 `worktree` 与 `base_sha`/`locked_test_sha256`，将 `attempt` 重置为 `0` 并消费一次 `human_hint`。

### 3.3 阶段 C：工兵上下文注入与隔离执行（`execute_worker_node`）

工兵 Prompt 由以下数据源拼装：
1. `spec.tasks`、`allowed_paths`、`acceptance_test_path`；
2. 若 `attempt == 0 and human_hint`：注入 `human_hint` 与最近 `last_error_log[-2000:]` 并标记 `REPAIRING`（消费后清空 `human_hint`）；
3. 若 `attempt > 0`：注入 `last_error_log[-3000:]` 与 `Repair Hint`（读取 `node_input["worker_summary"]` / `worker_changed_files`，缺失时从 `attempt_logs` 最新记录读取；`verify_python_node` 在前序失败时会优先把物理审计得到的 `audit.touched_files` 回写至 `node_input["worker_changed_files"]` 与 `attempt_logs.changed_files`，不因工兵返回非 JSON 纯文本而丢失改动记录）；
4. 若有 `spec.design_doc` 则追加架构设计正文；
5. `_build_ast_context(worktree, spec.allowed_paths)`：在 `worktree` 内实例化 `CodeGraph(worktree)` 并输出目标骨架与直接调用方签名；
6. `_load_okf_rules(worktree, repo_root, allowed_paths=spec.allowed_paths)`：从 `.adk/knowledge/rules/*.md` 过滤并注入匹配规则（未传入 `tags` 时，仅声明 `tags` 而无 `paths` 的规则会被跳过）。

执行隔离（`codebuddy_bridge.py` + `guard.py`）：
- 注入 `ADK_WORKER_TOKEN=run_id`、`ADK_ALLOWED_PATHS`、`ADK_LOCKED_TESTS`、`CODEBUDDY_IS_SANDBOX=1`；
- 移除父进程 `PORT` / `CODEBUDDY_PORT` 并强制 `--port 0`；
- 将 `PYTHONPATH` 置为 `worktree/src`（及 `worktree`）优先于主仓；
- Windows 下默认 `CODEBUDDY_USE_STDIN=true` 通过 stdin 传 Prompt；`spec.spike=True` 时 `max_turns = max(60, base * 4)` 且超时翻倍。

### 3.4 阶段 D：物理审计、真机验证与熔断决策（`verify_python_node`）

1. **物理沙箱与防篡改审计（`audit_worktree_changes`）**：
   - 校验 `acceptance_test_path` 存在且 SHA-256 等于 `locked_test_sha256`（篡改时自动从 `locked_test_commit_sha` 还原并记违规）；
   - 读取 `worktree` 真实 gitdir 下的 `info/exclude`，有非注释行即判定违规；
   - 执行 `git ls-files -v` 拦截 `h`/`s`/`S`/`m` 索引隐藏标记；
   - 汇总 `git status --porcelain -uall --ignored=matching` 与 `git diff --name-only locked_test_commit_sha..HEAD`，过滤 `_is_ephemeral_runtime_artifact`（含 `.adk/cache/`、`__pycache__`、`.pytest_cache`、`.ruff_cache`、`.mypy_cache`、`.hypothesis`、`.venv`、`venv`、`.pyc/.pyo/.pyd`、`.coverage*`），校验所有触碰文件均在 `allowed_paths` 且非 `PROTECTED_PATHS`。
2. **`zero_change` 空转检测**：对比 `(locked_test_commit_sha or base_sha)..HEAD` 与 `git status --porcelain -uall --ignored=matching` 时，同样过滤 `_is_ephemeral_runtime_artifact`（排除 `.adk/cache/cpg_cache.db`、`__pycache__`、`.pytest_cache` 等运行时产物），确保工兵未改任何有效文件时立即触发 `zero_change` 熔断。
3. **验证命令执行**：优先在 `{worktree}/.venv` ➔ `{repo_root}/.venv` ➔ `uv run --project` ➔ `PATH` 解析 `ruff`（仅当 `allowed_paths` 中存在现存路径或显式配置 `ADK_LINT_CMD` 时运行）与 `pytest`，以及可选 `ADK_REGRESSION_CMD`，子进程 `PYTHONPATH` 优先指向 worktree。
4. **日志与路由**：
   - 全绿：写入 `attempt_logs`（`worker_summary`、`worker_changed_files`），保持 `VERIFYING` 路由至 `passed -> publish_mr_node`。
   - 失败：调用 `compute_error_signature` 脱敏生成 16 位指纹并写回 `attempt_logs` 与 `runs`。触发 `zero_change`、连续两轮 `err_sig` 相同或 `next_attempt > ADK_MAX_RETRIES` 时 CAS 转 `ESCALATED` 并路由至 `escalated -> escalate_node`；否则转 `REPAIRING` 回流 `execute_worker_node`。

### 3.5 阶段 E：静态硬门禁、只读 Critic 与交付（`publish_mr_node`）

1. 自动 `git add -A -- . :(exclude).adk/cache` 并提交尚未提交的沙箱改动，随后将 `status` 转入 `REVIEWING`。
2. 对 `diff_base..HEAD`（`locked_test_commit_sha or base_sha`）中的所有变更 `.py` 文件逐个运行 `audit_ast_diff`（破坏性公共签名、语法错误、危险节点、函数/方法级 DFG 污点可达性）与 `audit_symbol_alignment`；内部异常严格 fail-closed 置 `ESCALATED`。
3. 审查裁决优先级：若注入了 `node_input` 中的 `approved` 结果则先过 `enforce_deterministic_verdict_guard`；否则若存在 `ast_findings` 直接由 AST 硬门禁一票否决；否则若开启 `ADK_ENABLE_CRITIC=1` 或配置了 `ADK_CRITIC_MODEL`，调用 `run_readonly_critic_review`（读取 `diff_base..HEAD` 即 `locked_test_commit_sha..HEAD` Diff，按 `ADK_MAX_DIFF_CHARS` 分文件预算截断；CLI 异常时默认 `ADK_CRITIC_FAIL_CLOSED=0` 降级放行，设为 `1` 则阻断）。
4. 本地落盘并提交 `.adk/specs/` 与 `.adk/audit/review_{run_id}.md`。
5. **发布前安全不变量**：在检查 `if not verdict.approved` 通过前，**绝不**执行 `save_distilled_knowledge`、**绝不**执行 `git push`、**绝不**创建 MR/PR，直接转入 `ESCALATED`。
6. 仅当 `verdict.approved == True` 时：若 `attempt > 0` 则从 `attempt_logs` 提取已解决的真实失败日志蒸馏写入 `.adk/knowledge/rules/learned_{run_id[:8]}.md`；当 Forge 启用时推送分支并创建/更新 PR/MR 与评审评论；记录分支 `head_sha` 后 CAS 转入 `MR_READY`（供 `flow_close_issue` 做祖先合入校验）。

---

## 4. 源码核查结果与已知残留边界（2026-11-28）

以下 4 项问题已于 `2026-11-28` 完成 TDD 加固修复（见 `tests/test_data_flow_optimizations.py`）：
- `verify_python_node` 的 `zero_change` 检测已过滤 `_is_ephemeral_runtime_artifact`，不受 worktree 内 `.adk/cache/cpg_cache.db` 干扰；
- `verify_python_node` 已优先将物理 `audit.touched_files` 回写至 `attempt_logs.changed_files` 与下一轮 `Repair Hint`；
- `SpecContract.to_okf_markdown` / `from_okf_markdown` 与 `okf.DevSpecMeta` 已支持 `spike`、`require_mutation_gate` 与 `design_doc` 无损往返；
- `publish_mr_node` 调用 `run_readonly_critic_review` 已切换为 `locked_test_commit_sha..HEAD`（即 `diff_base`）。

当前仍保留的边界：
1. **`flow_analyze_impact` / `flow_preflight_test` 不自动写库证据字段**：`impact_analysis` 与 `red_evidence` 仅当调用方将其写入 `SpecContract` 传给 `flow_lock_spec` 时才会持久化至 `runs.spec_json`。
2. **只读 Critic 默认降级策略**：默认 `ADK_CRITIC_FAIL_CLOSED=0` 在 CLI 异常时降级放行（`approved=True, confidence=0.7`）；生产强阻断需显式设为 `1`。
3. **其它已核实小边界**：
   - Resume 路径（`prepare_worktree_node` `else` 分支）会回填缺失的 `base_sha` 与 `locked_test_sha256`，但不会回填缺失的 `locked_test_commit_sha`。
   - `enforcement.py` / `enforce_flow.py` 拦截提示语写的参数名为 `flow_adopt(issue=..., branch=...)`，而 MCP 工具实际签名参数名为 `issue_iid`（`mcp_server.py:1521`）。
   - `ADK_PYTHON` 仅用于 `mutation.py` 变异沙箱，不作用于 `verify_python_node` 与 `flow_preflight_test`。
