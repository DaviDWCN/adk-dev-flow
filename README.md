# adk-dev-flow

面向 Python 业务仓库的**确定性 AI 研发工作流工具包**（基于 Google ADK 2.x + FastMCP + Git Worktree + GitLab）。

采用 **“指挥台（Commander）+ 隔离工兵（Worker）+ 不可绕过物理审计 + 独立 Critic 审查”** 架构，将 AI 编程收敛为可控状态机。

---

## 1. 作为独立工具安装（推荐）

无需把框架源码复制到每个业务仓库，一次安装全局复用：

```bash
# 方式 A：通过 uv tool 全局安装（推荐）
uv tool install git+https://github.com/DaviDWCN/adk-dev-flow.git

# 方式 B：本地源码开发安装
uv pip install -e ".[dev]"
```

安装后提供 3 个命令：
- `adk-dev-flow`：FastMCP Server 入口
- `adk-workflow-guard`：CodeBuddy `PreToolUse` 安全拦截钩子
- `adk-dev-flow-init`：向目标业务仓库一键写入薄 `.codebuddy/` 配置和 `.gitignore`

---

## 2. 接入目标业务仓库

在你的 Python 业务仓库根目录执行：

```bash
adk-dev-flow-init .
# 如需同时生成 .gitlab-ci.yml：
adk-dev-flow-init . --with-ci
```

生成的薄配置：
- `.codebuddy/skills/deterministic-dev/SKILL.md`
- `.codebuddy/mcp.json`
- `.codebuddy/settings.json`
- `.gitignore` 追加 `.adk/`

> 若未全局安装，也可在 `.codebuddy/mcp.json` 中改用 `uvx --from git+https://github.com/DaviDWCN/adk-dev-flow.git adk-dev-flow`。

---

## 3. 环境变量配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REPO_ROOT` | 当前目录 | 目标业务仓库根目录 |
| `GITLAB_URL` / `GITLAB_TOKEN` / `GITLAB_PROJECT_ID` | 空 | 内网 GitLab 配置（均配置后自动开启 Issue / MR 联动） |
| `GITLAB_CA_BUNDLE` | 空 | 企业自签根证书 PEM 路径（优先于 `GITLAB_SSL_VERIFY`） |
| `GITLAB_SSL_VERIFY` | `true` | TLS 校验开关（内网 PoC 可临时设 `false`，生产推荐配置 `GITLAB_CA_BUNDLE`） |
| `ADK_MAX_RETRIES` | `3` | 自动定向修复轮次上限（不含首次实现，即首次 + 最多 3 轮修复） |
| `ADK_LINT_CMD` | 自动探测目标仓库 `.venv` 的 `ruff check .` | 自定义 Lint 命令；设为 `skip` 可跳过 |
| `ADK_TEST_CMD` | 自动探测目标仓库 `.venv` 的 `pytest -q {test_path}` | 自定义验收测试命令（支持 `{test_path}` 占位符） |
| `ADK_REGRESSION_CMD` | 空 | 可选全量回归/覆盖率门禁（如 `pytest --cov=src --cov-fail-under=80 -q`） |
| `ADK_CRITIC_MODEL` | `CodeBuddyLlm()` | 覆盖 Critic Agent 使用的模型（如 `gemini-2.5-pro`） |
| `CODEBUDDY_BIN` | `codebuddy` | 本机 CodeBuddy CLI 可执行文件名或路径 |
| `CODEBUDDY_DISABLE_TOOLS_ARGS` | `["--allowedTools", ""]` | Critic 只读模式禁用工具的 CLI 参数（JSON 数组） |
| `CODEBUDDY_WORKER_EXTRA_ARGS` | `[]` | 传给 Headless 工兵的额外 CLI 参数（JSON 数组） |
| `CODEBUDDY_USE_STDIN` | `false` | 设为 `true` 时改为通过 stdin 传递长 Prompt |

---

## 4. 首次运行前：30 秒 `codebuddy` CLI 兼容性自检

不同版本的 CodeBuddy CLI 对禁用工具参数（`--allowedTools ""` vs `--tools ""`）及 `--json-schema` 的支持略有差异，建议首单前在终端实测一条最小命令：

```bash
codebuddy -p '返回 {"approved": true, "blocking_issues": [], "summary": "ok"}' \
  --output-format json \
  --no-session-persistence \
  --max-turns 1 \
  --allowedTools "" \
  --json-schema '{"type":"object","properties":{"approved":{"type":"boolean"},"blocking_issues":{"type":"array","items":{"type":"string"}},"summary":{"type":"string"}},"required":["approved","summary"]}'
```

如果本机版本不认 `--allowedTools ""`，只需在 `.codebuddy/mcp.json` 的 `env` 中配置对应的 `CODEBUDDY_DISABLE_TOOLS_ARGS`（例如 `'["--tools", ""]'`）即可，无需改代码。

---

## 5. 运行单元测试

```bash
# 使用 pytest（推荐）
pytest -q

# 或零依赖直接使用标准库 unittest 验证核心安全与状态机模块
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
