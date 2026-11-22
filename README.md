# adk-dev-flow

面向 Python 业务仓库的**确定性 AI 研发工作流系统**（基于 Google ADK 2.x + FastMCP + Git Worktree + GitLab / GitHub 双平台自适应 + OKF 规约体系）。

采用 **“指挥台（Commander）+ 隔离工兵（Worker）+ 不可绕过物理审计 + 独立 Critic 审查 + OKF 永久归档”** 架构，将 AI 编程收敛为强一致性受控状态机。

---

## 核心特性

- 🎨 **像素工坊卡通工位动态监控看板（Generative UI & MCP App）**：
  - 结合 Factorio / Peon / Pixel Agent 理念，将流水线收敛为 6 大工位动画（架构师、施工工兵、TDD 质检员、独立 Critic、交付卡车、熔断警报）。
  - 支持 Google Antigravity 原生 Aux Pane 侧边栏/对话内联渲染 (`flow_dashboard`)、自动化 SSE 流式推流与全屏独立 Web 仪表盘 (`adk-dev-flow-dashboard`)。
- 🐙/🦊 **GitLab & GitHub 双托管平台自适应**：
  - 自动识别当前仓库 remote（`github.com` vs `gitlab`）或环境变量，无缝适配拉取 Issue、创建 Draft PR/MR、追加 Critic 审查评论及解除 Draft。
  - 完全抽象的 `BaseGitForge` 架构，支持离线纯本地模式（零平台依赖）。
- 🛡️ **物理级沙箱与对抗防御**：
  - 采用 Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) 与 POSIX 进程会话，彻底终结子进程树孤儿与僵尸进程。
  - 上下文管理器显式释放 SQLite 句柄，杜绝 Windows WAL `WinError 32` 锁定。
  - 审计 Worktree 的 Git 内部标记（`--assume-unchanged`、`--skip-worktree`、`.git/info/exclude`、`sitecustomize.py` 越权），防止工兵伪造门禁通过。
- 🧪 **TDD Red 契约门禁**：
  - 强制验收测试在基线代码上执行失败（红灯校验），杜绝无断言空测试（Mock Passes）或假通过。
  - 支持 `extra_dependencies` 动态虚拟环境依赖同步。
- 📑 **OKF（Open Knowledge Format）Git 原生规约与合规审计**：
  - 规约以标准 Markdown + YAML Frontmatter 格式存储在 `.adk/specs/`，天然支持 Git Diff 与 Code Review。
  - 评审报告自动生成并提交到分支 `.adk/audit/review_{run_id}.md`，随 MR 形成不可篡改的证据链。
  - 项目工程规约（`.adk/knowledge/rules/`）自动注入工兵上下文，免除外部复杂向量库。
- 🌐 **Google ADK Web UI 原生可视化**：
  - 暴露标准 `root_agent`，可一键启动官方 ADK Web 控制台，实时查看 Trace 时间线、会话快照与工具状态。
- 🔄 **高可用多任务并发与抽象 Provider**：
  - CAS 原子状态转移保证零竞争分叉；支持 `flow_active_runs` 多任务监控与 `flow_abort_all` 一键清理。
  - 支持可插拔 Worker Runtime Provider（`CodeBuddyWorkerProvider`、`MockWorkerProvider`、自定义 Provider）。

---

## 1. 安装方式

```bash
# 方式 A：通过 uv tool 全局安装（推荐）
uv tool install git+https://github.com/DaviDWCN/adk-dev-flow.git

# 方式 B：本地源码开发安装
uv pip install -e ".[dev]"
```

安装后提供 4 个 CLI 命令：
- `adk-dev-flow`：FastMCP Server 入口
- `adk-dev-flow-dashboard`：像素工坊实时动画 Web 监控看板与 SSE 推流服务
- `adk-workflow-guard`：CodeBuddy `PreToolUse` 安全拦截钩子
- `adk-dev-flow-init`：向目标业务仓库一键脚手架薄配置、OKF 规约模版与知识库结构

---

## 2. 接入目标业务仓库

在你的 Python 业务仓库根目录执行：

```bash
adk-dev-flow-init .
# 如需同时生成 .gitlab-ci.yml：
adk-dev-flow-init . --with-ci
```

生成的工程结构：
- `.codebuddy/skills/deterministic-dev/SKILL.md`：指挥台标准作业流程
- `.codebuddy/mcp.json`：MCP 服务配置
- `.codebuddy/settings.json`：安全钩子绑定
- `.adk/specs/template.md`：OKF 规约模版
- `.adk/knowledge/index.md`：工程知识目录
- `.adk/knowledge/rules/engineering_rules.md`：工程规约基线
- `.gitignore`：追加 `.adk/`（忽略运行态，保留规约与知识）

---

## 3. 完整的 MCP 工具一览

服务注册于 Antigravity / CodeBuddy / Cursor 等 IDE，提供以下 12 个原子工具：

| 工具名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `flow_init` | 异步 | 初始化研发流，自动联动 GitLab / GitHub Issue 获取标题与描述 |
| `flow_lock_spec` | 同步 | 静态校验并锁定 Pydantic 结构化 Spec 契约与验收测试 |
| `flow_lock_spec_from_file` | 同步 | **直接读取 OKF Markdown 文件**（`.adk/specs/*.md`）并锁定规约 |
| `flow_dispatch` | 异步 | CAS 状态转移，后台拉起隔离 Worktree、执行工兵实现、TDD 校验与 Critic 审查 |
| `flow_status` | 同步 | 实时查询执行阶段、重试轮次、报错日志及 GitLab / GitHub PR/MR 链接 |
| `flow_dashboard` | 同步 | **打开像素工坊卡通工位动画看板**（支持 Antigravity 侧边栏/对话内嵌与实时渲染） |
| `flow_resume` | 异步 | 对处于 `ESCALATED` 熔断的流水线注入人工指令，在原隔离 Worktree 中恢复运行 |
| `flow_active_runs` | 同步 | 查询当前并发进行的所有活跃流水线状态 |
| `flow_list` | 同步 | 查询历史流水线运行记录（跨会话找回 `run_id`） |
| `flow_finalize` | 异步 | 验收完成后解除 GitLab / GitHub PR/MR 的 Draft 标记并清理隔离 Worktree |
| `flow_abort` | 异步 | 中止指定流水线，强杀子进程树并解除工作区防呆锁 |
| `flow_abort_all` | 异步 | 一键强杀所有正在执行的流水线并释放所有资源 |

---

## 4. OKF（Open Knowledge Format）规约驱动

无需通过 API 组装复杂的 JSON，直接在 `.adk/specs/` 下编写标准 Markdown：

```markdown
---
type: DevSpec
title: "feat: add distributed lock"
status: active
issue_iid: 42
target_branch: main
require_tdd_red: true
extra_dependencies:
  - "redis>=5.0.0"
allowed_paths:
  - "src/lock"
acceptance_test_path: "tests/test_lock.py"
tasks:
  - id: "T1"
    title: "Implement Redis Lock core"
    description: "Write RedisLock in src/lock/core.py"
    target_files:
      - "src/lock/core.py"
---

# feat: add distributed lock

## Summary
实现基于 Redis 的分布式锁与看门狗自动续期。

## Locked Acceptance Test (`tests/test_lock.py`)
```python
def test_lock_acquire_and_release():
    # 完整的 pytest 测试代码
    assert True
```
```

然后通过 `flow_lock_spec_from_file(run_id, ".adk/specs/issue-42.md")` 一键锁定并派发。

---

## 5. 🎨 像素工坊卡通工位动态监控看板（Generative UI & MCP App）

借鉴 Factorio 流水线、魔兽争霸 Peon 与 Pixel Agent 理念，系统将复杂的 Multi-Agent 研发状态机映射为 6 大生动的**卡通工人工位**：

1. **蓝图架构师（Architect）**：校验 OKF 规约与验收用例完整性，冻结契约。
2. **施工工兵（Worker Builder）**：黄色安全帽、手持铁锤快速敲击、火花四溅，安全沙箱中编写代码。
3. **TDD 质检员（Tester）**：身穿白大褂、手持变色试管，严格执行红灯预检与 Pytest 验收门禁。
4. **独立 Critic 审查官（Critic）**：侦探风衣，手持放大镜沿流水线射出激光扫描光束，审计越界修改与代码安全。
5. **MR 交付卡车（Delivery Truck）**：叉车装载打包好的代码货箱，自动创建/更新 GitLab MR 或 GitHub PR。
6. **熔断停机警报（Siren）**：全厂旋转红灯大作，触发熔断保护，呼叫人类工程师。

### 监控启动方式

- **命令行独立启动 Web 看板（支持毫秒级 SSE 实时推流）**：
  ```bash
  # 启动本地看板并在默认浏览器中打开 (默认 http://127.0.0.1:8080)
  adk-dev-flow-dashboard --open

  # 指定端口与特定运行轮次
  adk-dev-flow-dashboard --port 8888 --run-id <run_id>
  ```
- **IDE 内嵌与侧边栏（Antigravity / CodeBuddy）**：
  在对话中调用 `flow_dashboard(run_id=...)`，系统自动利用 `ui://adk-dev-flow/dashboard.html` 将动态看板渲染在 Aux Pane 侧边栏或对话内嵌流中。

---

## 6. 启动 Google ADK Web UI 可视化控制台

在当前项目目录执行：

```bash
uv run adk web src --port 8000
```

打开浏览器访问 **http://127.0.0.1:8000** 即可体验：
- **Agent 图拓扑与对话**：直接在 Web 界面与 `adk_dev_flow` 协调器对话。
- **Session & Traces 链路**：可视化审查每一次步骤转移、时间开销与调用结果。
- **Artifacts 制品浏览器**：查看每次运行生成的 Diff 补丁与 OKF 评审报告。

---

## 7. 环境变量与高级配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REPO_ROOT` | 当前工作目录 | 目标业务仓库根目录 |
| `GIT_FORGE_PROVIDER` | `auto` | 托管平台覆盖开关（`github` / `gitlab` / `none` / `auto`） |
| `GITHUB_TOKEN` / `GH_TOKEN` | 空 | GitHub 个人访问令牌（PAT），配置后支持 Issue 联动与创建 PR |
| `GITHUB_REPOSITORY` | 自动从 git remote 解析 | GitHub 仓库路径（如 `owner/repo`，未配置时自动从 `origin` 提取） |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API 根路径（支持 GitHub Enterprise） |
| `GITLAB_URL` / `GITLAB_TOKEN` / `GITLAB_PROJECT_ID` | 空 | 内网 GitLab 联动机关（均配置后自动同步 Issue 与推送 MR） |
| `GITLAB_CA_BUNDLE` | 空 | 企业自签根证书 PEM 文件路径 |
| `GITLAB_SSL_VERIFY` | `true` | TLS 校验开关 |
| `ADK_MAX_RETRIES` | `3` | 工兵自愈重试上限轮次 |
| `ADK_WORKER_PROVIDER` | `codebuddy` | 工兵后端类型（`codebuddy` / `mock`） |
| `ADK_WORKER_MODEL` | 空（跟随 CLI 默认） | 工兵代码生成模型（推荐 `deepseek-v4.1-flash`） |
| `ADK_CRITIC_MODEL` | `CodeBuddyLlm()` | Critic 独立审查模型（推荐 `glm-5.3-flash`） |
| `CODEBUDDY_BIN` | 自动探测 `PATH` | CodeBuddy CLI 可执行文件路径 |
| `CODEBUDDY_USE_STDIN` | `false` | 启用 stdin 管道传输 Prompt（消除 Windows 换行与引号转义风险） |
| `ADK_SKIP_TDD_RED` | `0` | 设为 `1` 可临时跳过 TDD Red 阶段门禁 |
| `ADK_LINT_CMD` | 自动探测 `.venv` 的 `ruff check .` | 静态检查命令；设为 `skip` 可跳过 |
| `ADK_TEST_CMD` | 自动探测 `.venv` 的 `pytest -q {test_path}` | 验收测试命令模板（支持 `{test_path}` 占位符） |
| `ADK_REGRESSION_CMD` | 空 | 可选全量回归/覆盖率门禁 |

---

## 8. CodeBuddy 模型选型与评测基准套件

针对业务仓库的确定性 AI 流水线，`adk-dev-flow` 提供了专门面向流水线任务的 **5 维自动化评测套件**（位于 `scripts/benchmark_models.py`），考核重点包含：

1. **JSON Schema 遵从度**：严格输出结构化 JSON，零多余自然语言与 Markdown 标记；
2. **白名单防御与边界感**：诱导修改锁定的测试用例时能否坚决阻断；
3. **Python TDD 编码 Pass@1**：复杂算法（如 TokenBucket 限流器）一次性通过 Pytest 验收测试的成功率；
4. **Critic 审查安全否决率**：敏锐查杀硬编码凭据与明文比对漏洞并一票否决；
5. **端到端生成延迟**：平均耗时与吞吐稳定性。

### 实测模型天梯榜

| 模型名称 | 平均耗时 | JSON 遵从 | 边界意识 | TDD 编码质量 | Critic 否决 | 推荐角色 |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **`deepseek-v4.1-flash`** | **~4.2s** | 100% | 100% | **优** (边界处理完备) | **100%** (精准定位) | 👑 **首选 Worker (编码工兵)** |
| **`glm-5.3-flash`** | ~6.8s | 100% | 100% | **优** (解释细致) | **100%** (审查极其敏锐) | 🛡️ **首选 Critic (独立盲审)** |
| **`minimax-m3`** | ~6.2s | 95% | 100% | 中 (边缘用例有疏漏) | 100% | 备用 Worker |
| **`glm-5v-turbo`** | ~10.3s | 90% | 100% | 中 | 100% | 耗时偏长，适合多模态任务 |

> **💡 最佳实践：“双模型协作制”**  
> 建议在工程中采用双模型分工：
> - 工兵（Worker）配置 `ADK_WORKER_MODEL=deepseek-v4.1-flash`，极致追求生成速度与 TDD 一次性通过率；
> - 审查官（Critic）配置 `ADK_CRITIC_MODEL=glm-5.3-flash`，利用其更为严谨详尽的审核逻辑确保交付安全。

### 一键运行模型基准评测

```bash
# 自动评测本地支持的候选模型并输出天梯榜得分
uv run python scripts/benchmark_models.py
```

---

## 9. 运行自动化测试

项目内建 52 个全量单元测试与物理对抗用例（涵盖 GitHub/GitLab 双平台自适应、实时动画看板与 SSE 端点、Job Object 树强杀、NTFS 锁争用、Git 索引作弊、OKF 序列化、TDD 契约等）：

```bash
# 执行全量单元与对抗测试
uv run --no-sync python -m unittest discover -s tests -v
```
