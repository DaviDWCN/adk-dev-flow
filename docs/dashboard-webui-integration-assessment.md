# 评估：adk-dev-flow 像素工坊看板集成到 CodeBuddy CLI Web UI

> 调研日期：2026-11-23
> 结论先行：**技术上完全可行，且项目已具备约 70% 的协议基础**。主要差距不在"能不能嵌"，而在"嵌入后数据怎么来"——当前看板依赖独立 HTTP 服务的 `/api/*` 端点，而 MCP Apps widget 跑在异源沙箱 iframe 里，无法直接访问这些相对路径端点。需要一次中等规模改造（1~3 天）。

---

## 0. 最小验证结果（2026-11-23 实测，已通过）

用 `codebuddy --serve --port 8321 --auth none` 启动 Web UI，在对话中通过 ToolSearch → DeferExecuteTool 调用 `flow_dashboard`，用 Playwright 驱动浏览器实测：

| 验证点 | 结果 |
|---|---|
| 工具调用成功返回 structuredContent | ✅ `{"status":"IDLE","message":"暂无活跃任务"}` |
| Web UI 渲染出 MCP Apps widget iframe | ✅ `src=http://127.0.0.1:8321/sandbox_proxy.html`，`sandbox="allow-scripts allow-same-origin allow-forms"`，878×600 可见——与 `mcp-apps.md` 描述的 host 行为完全一致 |
| 看板 HTML 完整渲染 | ✅ 截图确认：6 大工位 SVG 拓扑（蓝图架构师/施工工兵/Critic 等）、4 道门禁卡片、HITL 护栏条、"自动演练流水线"按钮全部可见，样式无错乱 |
| 外部 CDN（gstatic tailwind）未被 CSP 拦截 | ✅ 样式正常加载（resource 未声明 csp 时 host 未注入严格默认策略） |
| 状态条显示结构化数据 | ✅ 显示 STANDBY/活跃任务等状态（widget 内部对 IDLE 的呈现），数据链路已通 |

**结论：集成可行性从"推断"升级为"实测确认"。** 方案 1 的前提成立，剩余工作即评估报告第 3 节所述的数据层改造。

注：验证会话中该 widget 对应的看板显示的是 `adk/issue-42-feat` 分支与活跃任务信息，而非本次调用的 IDLE 快照——与评估第 2 节差距 3（异步状态不自动刷新）一致，属已知待改进项，不影响渲染结论。

---

## 0. 最小验证结果（2026-11-23 实测 ✅ 通过）

通过 `codebuddy --serve --port 8321 --auth none` 启动 Web UI，用 Playwright 驱动浏览器让会话调用 `flow_dashboard`，实测结论：

| 检查项 | 结果 |
|---|---|
| Widget iframe 渲染 | ✅ 双层 iframe 出现：外层 `e1142`（mcp-ui AppRenderer 容器）→ 内层 `f1e2`（host 自供 sandbox_proxy.html，异源沙箱） |
| 看板 DOM 注入 | ✅ 看板完整渲染："ADK 确定性研发流水线 · 全景监控驾驶舱"、STANDBY 待机状态、repo/分支信息 |
| 视觉样式 | ✅ 彩色卡片、流水线门禁图、熔断机制区块全部正常显示（截图确认），无白屏 |
| structuredContent 推送 | ✅ toolResult `{status: IDLE, message: 暂无活跃任务}` 到达 tool result 区块 |
| 浏览器控制台 | ✅ 0 errors |
| CSP | ✅ 未声明 csp 的情况下 gstatic tailwind 仍成功加载（与评估时的担忧不符，实测无障碍） |

**重要澄清**：widget 中显示的 `adk/issue-42-feat` 分支等是看板 HTML 内置的演示数据（`dashboard.html` 自带模拟循环），并非真实流水线状态——独立服务的 `/api/*` 在沙箱内不可达，这印证了评估的核心差距：**渲染管线全通，数据链路待改造**（即方案 1 的工作量所在）。

**验证结论**：方案 1 的前提成立，CodeBuddy Web UI 对 MCP Apps 的支持是真实可用的；剩余工作集中在数据链路（ext-apps App 接入 + ontoolresult/轮询）。

---

## 1. CodeBuddy Web UI 的两条扩展通道

调研内置文档（`web-ui/docs/cn/cli/`）确认，把自定义 UI 送进 Web UI 有两条官方通道：

### 通道 A：MCP Apps（`io.modelcontextprotocol/ui` 协议扩展）
- 文档：`mcp-apps.md`
- 机制：MCP server 把一个 HTML 作为 **UI Resource** 暴露（`ui://` URI + MIME `text/html;profile=mcp-app`），在某个 tool 定义的 `_meta.ui.resourceUri` 里引用它。Host 看到标记后，把 HTML 装进**异源 sandbox iframe**，在对话气泡里渲染成可交互 widget。
- 双向通信：widget 通过 `@modelcontextprotocol/ext-apps` 的 `App` 实例走 JSON-RPC over postMessage。Widget → Host 可调 `app.callServerTool` / `app.readServerResource` / `app.sendMessage` 等；Host → Widget 推 `ontoolresult` / `onhostcontextchanged`。
- 适用范围：**Web UI（--serve）✅、IDE 内嵌 Web UI（VSCode/Fusion/JetBrains）✅、终端 TUI ❌（自动文本降级）、print 模式 ❌**。
- 大小限制：HTML > 256 KB 时 host 不预取；当前看板 56 KB，远低于阈值，无障碍。

### 通道 B：Mods（`ui.render` 生成式 UI）
- 文档：`mods.md`
- 机制：写一个 TypeScript Mod（`plugin.json` + `hooks/register.ts`），通过 `on('ui.render', { component: ... })` 接管渲染，返回元素树（闭集：Box/Text/Button/Input/Select/Link/Code/Markdown/Image/Svg/Raster/Client）。Web UI 与 TUI 共用同一套 ACP 扩展消息（`_codebuddy.ai/mod*`）。
- 适合：常驻面板、输入框控制、工具结果自定义渲染。元素表是闭集，复杂 SVG 动画看板用这套原语重画成本很高。
- 需要 JSX 转译器通道；跨端（TUI/Web/Desktop）需各自验证。

### 通道选择
**选通道 A（MCP Apps）**。当前项目本就是 MCP server，`flow_dashboard` 已声明了 UI Resource，走 A 是原生路径；通道 B 适合轻量控件，不适合复刻整套动画看板。

---

## 2. 当前实现与 MCP Apps 协议的差距

项目现状（`mcp_server.py:591-620`）：

```python
APP_URI = "ui://adk-dev-flow/dashboard.html"

@mcp.resource(
    APP_URI,
    name="ADK Flow Cartoon Dashboard",
    mime_type="text/html;profile=mcp-app",          # ✅ 正确的 UI Resource MIME
    meta={"ui": {"prefersBorder": False}},
)
def get_dashboard_resource() -> str:
    return get_dashboard_html()                       # ✅ 返回完整 HTML

PANE_META = {
    "ui": {"resourceUri": APP_URI},                   # ✅ tool 关联 UI Resource
    "com.google.antigravity/ui": {...},               # ⚠ Antigravity 专有扩展，CodeBuddy 不识别
}

@mcp.tool(name="flow_dashboard", title="...", meta=PANE_META)
def flow_dashboard(run_id=None) -> dict:
    return {
        "content": "ADK 像素工坊看板已就绪...",         # ✅ 文本兜底（TUI/print 用）
        "structuredContent": status_info,             # ✅ 实时数据（协议会推给 widget）
    }
```

**已符合（约 70%）**：
- UI Resource URI 以 `ui://` 开头 ✅
- MIME 为 `text/html;profile=mcp-app` ✅
- tool 的 `_meta.ui.resourceUri` 关联正确 ✅
- toolResult 带 `structuredContent` 实时数据 ✅
- HTML 56 KB < 256 KB，host 会预取 ✅
- 已有 postMessage 监听（看板末尾 `window.addEventListener('message', ...)`） ✅（但未接 ext-apps `App` 的生命周期协议）

**关键差距（阻碍直接嵌入）**：

1. **数据来源不兼容**（核心障碍）
   - 看板当前通过 `fetch('/api/status')`、`fetch('/api/runs')`、`new EventSource('/api/events')` 从**独立 dashboard 服务**（`adk-dev-flow-dashboard`，默认 127.0.0.1:8080）拉数据（`dashboard.html:812/822/846`）。
   - MCP Apps widget 跑在**异源 sandbox iframe**，相对路径 `/api/*` 指向 host 页面 origin，不会命中 dashboard 服务；且 CSP 默认只允许 `'self'` + 声明的域名，`127.0.0.1:8080` 未声明会被拦截。
   - 结果：嵌入后动画能渲染，但**数据一直是 IDLE / 空**，拿不到真实流水线状态。

2. **未使用 ext-apps App 客户端协议**
   - 当前只监听裸 `message` 事件的 `structuredContent`/`status` 字段，不是 `app.ontoolresult` 标准回调，也没有 `app.connect()` / `ui/initialize` 握手。
   - Host 推送 toolResult 走的是标准 ext-apps 通道，当前代码不一定能正确接收。

3. **`structuredContent` 只有单次快照**
   - `flow_dashboard` 返回的是调用那一刻的 `status_info`。流水线是异步推进的（dispatch 后后台跑），widget 打开后不会再自动收到更新——除非 widget 主动调 `app.callServerTool('flow_status', ...)` 轮询，或新增一个 SSE/MCP 订阅通道。
   - 当前 dashboard 的独立服务靠 SSE 推实时更新，这条链路在 widget 里断了。

4. **CSP / 外部资源**
   - 看板用了 `https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js`（外部脚本）。MCP Apps 的 CSP 默认拦截未声明域名，需在 resource 的 `_meta.ui.csp.resourceDomains` 里加 `https://www.gstatic.com`，否则样式全丢。

5. **`com.google.antigravity/ui` 扩展**
   - 这是 Antigravity 的专有 pane meta，CodeBuddy 不认。不影响对话内联渲染，但 CodeBuddy 侧不会有 `aux_pane`/`global` 入口。若要常驻侧边栏，需另走 Mods 的 `$.ui.open` / pane 机制，或用 `app.requestDisplayMode({ mode: 'pip' })` 让用户切画中画。

---

## 3. 集成方案（按推荐度）

### 方案 1（推荐）：把看板改造成纯 MCP Apps widget
工作量：中等（约 1~3 天）

1. **引入 ext-apps**：HTML 里 `import { App } from 'https://esm.sh/@modelcontextprotocol/ext-apps@1'`，`const app = new App({ autoResize: true }); await app.connect();`
2. **数据改为结构化推送**：
   - `flow_dashboard` 的 `structuredContent` 已是现成的状态快照，widget 在 `app.ontoolresult` 里 `handleLiveUpdate(result.structuredContent)` 即可首屏渲染。
   - 实时更新：`app.callServerTool('flow_status', { run_id })` 定时轮询（MCP Apps 反向调用默认弹授权，但 `-y`/BypassPermissions 或 session 内选过"始终允许"后直通；可用 `app.updateModelContext` 把状态喂回主对话）。
   - 或在 idle 时调用 `app.readServerResource` 拉取只读状态（免授权）。
3. **删掉对独立服务的依赖**：移除 `fetch('/api/*')` / EventSource，或改为**降级路径**——探测到在 widget 环境（`window.__MCP_APP__` / `app` 存在）走 MCP 通道，否则仍走独立服务，保留两种用法。
4. **补 CSP**：resource `_meta.ui.csp` 加 `resourceDomains: ['https://www.gstatic.com', 'https://esm.sh']`，`connectDomains` 同理。
5. **主题适配**：按 `mcp-apps.md` 的双层兜底改（`color-scheme: light dark` + `light-dark()` + `app.onhostcontextchanged`）。

优点：一次改造，Web UI 对话内直接渲染，且 IDE 插件内嵌 Web UI 也生效；TUI 自动降级为文本，无需维护两套。
代价：放弃独立服务的 SSE 毫秒级推送，改为轮询（秒级）；或额外做 MCP streaming。

### 方案 2：保留独立 Web 服务，Web UI 里用 iframe/外链打开
工作量：小

- 看板继续用 `adk-dev-flow-dashboard --port 8080` 跑独立服务，CodeBuddy Web UI 里通过 `app.openLink('http://127.0.0.1:8080/')` 弹出浏览器新页。
- 这**不是"集成进页面"**，只是从 Web UI 跳出去；不满足"嵌入网页之中"的目标。仅作过渡。

### 方案 3：Mods 常驻面板
工作量：大

- 写 TypeScript Mod，用 `$.process.run` 起 dashboard 服务，`$.http.fetch` 拉 `/api/status`，`ui.render` + `ui.renderFold` 画到具名 pane。元素表闭集不含"任意 HTML/SVG 动画"，6 大工位的像素动画需用 Svg/Box 原语重画，或 `<Client>` 承载（文档未给 Client embed 外部 URL 的示例，需验证）。
- 优点：可做常驻侧边栏；缺点：重画成本高、动画能力受限、需维护 TS 工具链。

---

## 4. 风险与约束

| 风险 | 说明 | 缓解 |
|---|---|---|
| 反向调用授权弹窗 | widget 调 `flow_status` 会弹授权，打断观看体验 | `-y` 启动 / session 内"始终允许"；或只用 `ontoolresult` 推送，不反向调用 |
| 异步状态不自动刷新 | 用户在 widget 里看不到流水线推进 | 轮询 `flow_status` 或新增 MCP streaming/订阅 |
| 外部 CDN 依赖 | gstatic tailwind 需 CSP 放行；内网环境不可用 | 把 CSS 内联进 HTML（56 KB 会增大但仍远低于 256 KB） |
| TUI 降级 | 终端模式看不到动画，只有文本 | 符合预期，独立 Web 服务仍可用 |
| 历史消息占位 | 刷新页面后旧 widget 不自动加载 | 协议默认行为，用户点开才装载 |
| 多项目 | widget 绑定单个 MCP server（其 REPO_ROOT 已按项目 env 隔离） | 每项目一会话，天然隔离 |

---

## 5. 建议

1. **走方案 1**，把 `flow_dashboard` 做成合规的 MCP Apps widget——这是官方原生路径，改动集中在 `dashboard.html` 数据层和 `mcp_server.py` 的 resource meta，不动核心流水线逻辑。
2. 先做一个**最小验证**：新会话里调用 `flow_dashboard`，确认 CodeBuddy Web UI 是否真的渲染出 iframe widget（当前 meta 已声明，可能已经能显示静态画面）。这是判断投入的前提——如果 CodeBuddy 当前版本对此支持不完整，方案 1 的收益会打折。
3. 独立 Web 服务（`adk-dev-flow-dashboard`）建议保留，作为多项目集中监控和 TUI 用户的入口，与 widget 并存而非替换。
4. 若需要"常驻侧边栏"效果，再评估方案 3 的 Mods 面板，但把它当作二期。

**一句话**：能集成，且已经半只脚踏进门里了；剩下的主要是把看板的数据获取从"独立 HTTP 服务"改成"MCP Apps 结构化推送 + 反向工具调用"。
