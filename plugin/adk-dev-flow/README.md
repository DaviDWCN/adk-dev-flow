# ADK Deterministic Dev Flow Plugin (v0.2.3)

面向所有 Agent 的确定性研发流水线插件包。

## 核心特性
- 🛠️ **22 个 FastMCP 原子工具**：包括 `flow_init`, `flow_analyze_impact`, `flow_preflight_test`, `flow_pre_critic`, `flow_pre_critic_from_file`, `flow_lock_spec`, `flow_dispatch`, `flow_dashboard` 等。
- 🌲 **AST 代码图谱与影响面分析**：自动推导修改的爆炸半径与测试集。
- 🎨 **像素工坊工位动画看板**：支持 Aux Pane 侧边栏（Sidecar / MCP App）及内联与独立 Web 渲染。
- 🛡️ **物理沙箱与 TDD Red 强制契约**：杜绝非白名单越权修改与非确定性 AI 输出破坏生产。

## 安装与使用
当此插件启用后，Agent 自动获取 `deterministic-dev` 研发技能、像素工坊看板与强一致性工程规范。
