# 评估：以插件（Mod）方式在 CodeBuddy Web UI 侧边栏常驻像素工坊看板

> 调研日期：2026-11-23 · 基于本机 CLI v2.x 打包代码（`codebuddy.js` 35MB 主 bundle）与官方类型定义 `codebuddy-code.d.ts` 逐层验证
> 结论先行：**可行，且存在一条官方完整链路**：`$.ui.open()` → `ui.render {component:'Pane'}` → Web UI `<aside class="mod-dock">` 侧边面板。数据可直接用 `$.mcp.call('adk-dev-flow', 'flow_status')` 轮询，**无需独立 HTTP 服务、无需改 MCP server**。中等规模改造（2~4 天），关键不确定点是 SVG/CSS 动画在 Web UI 渲染器的保真度，需一次实测。

---

## 1. 机制全景（从打包代码逆向验证的完整链路）

```
Mod (hooks/register.ts)
  │  $.ui.open({ id:'pixel-forge', title:'像素工坊看板' })      → openModPane
  │  on('ui.render', {component:'Pane'}, ...)                   ← agent dispatch（surface='cbc-webui'）
  │     返回元素树（Box/Text/Svg/Button/Markdown/Client…）
  ▼
agent 序列化 → extNotification('_codebuddy.ai/modRenderFold',
  { instanceKey:'Pane:pixel-forge@cbc-webui', tree })
  ▼
Web UI (use-acp-*.js) → store.setFold → <aside class="mod-root mod-slots mod-dock">
  placement==='dock' 时渲染进侧边 dock（宽度 calc(60ch+3ch)，约 900px 级）
  ▼
用户点击按钮 → '_codebuddy.ai/modSlotPress' {plugin, scope:'Pane:<id>@cbc-webui', element}
  → agent dispatch('ui.press') → Mod 的 onPress 闭包执行 → 改状态 → $.ui.invalidate('ui.render') 重绘
```

关键代码证据（均在打包 JS 中定位到）：

| 环节 | 证据 |
|---|---|
| Web UI slot scope | `modSlotPress` 校验正则 `/^(AbovePrompt\|Pane:[^@]+)@cbc-webui$/` |
| Web UI surface 名 | `snapshotModSlots("cbc-webui", columns)` |
| Dock 侧边面板 | `index-*.js` 中 `_be()`：`<aside class="mod-root mod-slots mod-dock" data-mod-dock={id} style={{width: calc(${columns??60}ch + 3ch)}}>`，条件 `slots.pane.placement==='dock'` |
| 非 dock 位置 | `Q5()`：pane 以 `mod-slot-band` 渲染在输入框上方（AbovePrompt） |
| 元素渲染器 `Y5` | 支持 `Text/Code/Markdown/Link/Button/Box/Fragment/Svg/Client`；`Svg → ww()`；`Client → bw`（插件客户端模块本地渲染） |
| 交互回传 | `modSlotPress` → `dispatch('ui.press',{surface,component:'Pane',requestId:paneId,scope})` |
| 断线重连 | `modRenderRequest`（客户端 pull 首帧）+ `snapshotModSlots`（agent 推快照）——晚到的浏览器也能看到当前状态 |

另有 `client.contributes.dockTabs`（最多 16 个 dock 入口 + 32 个 commands，中英文标签）的声明式通道，当前在 web-ui 侧只见声明解析、未见内容渲染实现——**建议以 `$.ui.open` 运行时链路为主**，dockTabs 作为入口快捷方式备用。

## 2. 数据链路：比 MCP Apps 方案更顺

Mod 的 `$` 能力面自带两条现成数据通道（`mods.md` `$` API 速查）：

1. **`$.mcp.call('adk-dev-flow', 'flow_status', { run_id })`** —— 直接调本插件 MCP server 的工具，拿 `structuredContent` 状态快照。配合 `$.clock.every(1000, ...)` 轮询 + `$.ui.invalidate('ui.render')`，即可秒级刷新 dock。
2. `$.http.fetch` —— 也可打独立 dashboard 服务的 SSE/REST，但既然 MCP 直连可用，没必要。

对比 MCP Apps widget 方案（`dashboard-webui-integration-assessment.md` 方案 1）：widget 的反向 `tools/call` 每次弹授权（或依赖 session 缓存），而 **Mod 的 `$.mcp.call` 走 agent 侧受信通道，无授权弹窗**；且 Mod 方案完全不用动 `dashboard.html` 和 `mcp_server.py`。

## 3. 渲染路线二选一

### 路线 A：`Svg` 元素树（推荐起步）
- `ui.render` 返回 `Svg` 元素（现有看板本就是 `viewBox="0 0 960 300"` 的 SVG 拓扑图），配合 `Box`（flex 布局→CSS）做外框、`Text` 显示 run 信息、`Button` 做 run 切换/abort。
- 状态驱动：每个 `$.clock.every` tick 更新树。
- 风险：`Svg` 渲染器 `ww()` 对 **CSS keyframes 动画**（看板用 8 组 `@keyframes`：hammer-swing、truck-vibe、alarm-spin 等）的支持程度未知——序列化闭集按元素属性走，CSS 动画若以 `style` 字符串/子元素传递可能被剥除。**需一次实测**：写 10 行 Mod 画一个带 `<animate>`/style 动画的 SVG 到 dock，看动不动。
- 降级：不动动画，只做"状态位图"（工位颜色/徽章/阶段指示），保信息量、弃动画，零风险。

### 路线 B：`Client` 元素 + 客户端 TSX 模块（动画保真上限）
- `<Client key="dash" module="./dashboard-client.tsx" props={status} />` —— 该 TSX 由 **Web UI 浏览器本地加载执行**（`bw` 组件），`props`（≤100k 字符）从 agent 侧传入，实例状态在客户端保持。
- 可以把 `dashboard.html` 的动画 CSS/JS 移植成 React 组件，**动画保真度最高**；props 只传状态，动画逻辑全在本地，天然 60fps 不经 agent。
- 风险：`Client` 在 `Elements` 表的 terminal/desktop 有定义，Web UI 渲染器里也有分支（`case 'Client': return bw(...)`），但 `mcp-apps` 式的沙箱边界不适用——客户端模块跑在 Web UI 主页面上下文，需注意 CSP/作用域；文档标注 Mods 整体 **Beta**，Client 的 webui 实现成熟度需实测。

**建议：A 起步验证链路（半天），B 作为二期动画升级。**

## 4. 约束与风险

| 项 | 说明 | 缓解 |
|---|---|---|
| Mods 为 Beta | API 仍在演进（`mods.md` 明示） | 链路已实测存在于当前版本；锁定 CLI 版本测试 |
| Hook 预算 10s | 单次 hook 执行限时 | 轮询放 `$.clock.every`（回调非 hook 链），render hook 只做树构造 |
| 双面适配 | `ui.render` 同时会被 terminal（TUI）与 cbc-webui 调用 | 按 `e.surface` 分支：terminal 给紧凑文本树，webui 给 SVG/Client |
| TUI dock 列宽 | TUI 需 ≥110~144 列才画出 dock | 不影响 Web UI；TUI 自动落回 AbovePrompt inline |
| Dock 宽度 | Web UI 默认 60ch ≈ 观感 ~700-900px，可拖拽调宽（用户拖动优先） | SVG 用 `viewBox` 自适应缩放 |
| 资产限制 | "scripts, HTML and SVG are not served as assets"——Mod 不能挂任意 HTML 文件 | 路线 B 的 Client 模块是 TSX 源码编译通道，不受此限；外部 tailwind CDN 不可用，样式用内联 style/emotion 式写法 |
| 宿主关闭 Mods | `CBC_ENABLE_FUNCTION_HOOKS=0` 或 disableAllHooks 时全链路失效 | 独立 Web 服务兜底（多项目监控场景本来就要保留） |

## 5. 与既有方案的定位关系

| 方案 | 位置 | 实时性 | 动画保真 | 工作量 | 状态 |
|---|---|---|---|---|---|
| 独立 Web 服务（现有） | 浏览器独立页 | SSE 毫秒级 | 100%（原生 HTML） | 0 | 已上线，多项目监控主力 |
| MCP Apps widget（方案 1） | 对话气泡 inline/fullscreen/pip | 轮询/推送 | 高（原生 HTML 沙箱） | 1~3 天 | 渲染已实测通过，数据链路待改造 |
| **Mod dock 侧边栏（本报告）** | **Web UI 常驻侧边 dock** | `$.mcp.call` 秒级轮询 | A：中（Svg 树）· B：高（Client TSX） | 2~4 天 | 链路代码级验证完毕，动画保真度待实测 |

三者不互斥：独立服务管多项目总览；widget 管"对话内即时看"；dock 管"编码时一直瞄"。

## 6. 建议实施路径（PoC 半天 → 二期）

1. **PoC（半天）**：新建 `mod/pixel-forge-dock/`（`.codebuddy-plugin/plugin.json` + `hooks/register.ts`），`session.start` 时 `$.ui.open`，`ui.render` 返回静态 SVG 树 + 一个 Button；`codebuddy --serve --plugin-dir ./mod/pixel-forge-dock` 打开 Web UI 验证 dock 出现、SVG 绘制、按钮回环。**同场加测 CSS 动画存活情况**——此结果决定路线 A/B。
2. **一期（1~2 天）**：接数据——`$.clock.every(1000)` 轮询 `$.mcp.call('adk-dev-flow','flow_status'/'flow_active_runs')`，映射状态机到工位着色/阶段徽章；`e.surface` 分支适配 TUI 紧凑文本。
3. **二期（1~2 天，视 PoC 动画结果）**：路线 B，把 `dashboard.html` 的 SVG+CSS 动画移植为客户端 TSX 组件，props 传状态，动画本地跑。
4. 发布形态：Mod 目录随本仓库走，`--plugin-dir` 加载；后续可进 marketplace。

**一句话**：插件方式不仅可行，而且比 MCP Apps widget 更契合"侧边栏常驻"这个诉求——官方 dock 面板、无授权弹窗、MCP 直连数据、不动现有代码；唯一要赌的是 SVG 动画保真度，PoC 半天即可见分晓。

---

## 7. PoC 实测结果（2026-11-23，全部通过）

`mod/pixel-forge-dock/` 已实现并通过 `plugin test`（3/3）与 Web UI 宕机实测：

| 验证项 | 结果 | 证据 |
|---|---|---|
| dock 面板出现 | ✅ | `aside.mod-dock` count=1，右侧常驻栏，含标题/关闭按钮 |
| Svg 元素绘制 | ✅ | SMIL 旋转蓝方块 + CSS pulse 橙圆 + CSS spin 绿方块 + 静态灰块全部可见 |
| **SMIL/CSS 动画存活** | ✅ | 相隔 800ms 两帧截图字节差异（frameDiffers=true），动画在跑 |
| 按钮回环 | ✅ | 点击 +1 → HTTP `modSlotPress` → 服务端 `ui.press` 链 → `$.store.set` → SSE `modRenderInvalidate` 推送 → 面板重绘 `count = 1` |
| 单元测试 | ✅ 3/3 | `codebuddy plugin test ./mod/pixel-forge-dock`（session.start 开面板 / SVG 含动画源码 / press 计数回环） |

### 关键发现（踩坑记录，接一期必须知道）

1. **Mods 必须显式开启**：本构建 `isModHostAllowed()` 硬编码 false，`functionHooksVerdict` 落到 `host-veto`——除非设置 `CBC_ENABLE_FUNCTION_HOOKS=1`（或 `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`）。之前 serve 会话静默不加载 Mod 的根因就是这个，且无任何日志。
2. **Mod 目录来自 `CODEBUDDY_PLUGIN_DIRS` 环境变量**（path 分隔符分隔多个），`--plugin-dir` 只对子 agent 分发生效，主进程 serve 不读它。正确启动方式：
   ```bash
   CBC_ENABLE_FUNCTION_HOOKS=1 CODEBUDDY_PLUGIN_DIRS="d:/workbench/AgentHarness/adk-dev-flow/mod/pixel-forge-dock" \
     codebuddy --serve --port 8322 --auth none --permission-mode bypassPermissions
   ```
3. **元素构造器 children 必须放 props 里**：`el.Box({ flexDirection, children: [...] })`——可变参数形式 `el.Box(props, child1, child2)` 会静默丢弃 children（测试桩帮你抓住的这个坑，生产环境同样存在）。
4. **Web 面板 Button 点击走 `onPress` 不走的路径已证实**：实际链路是 POST `_codebuddy.ai/modSlotPress {plugin, scope:"Pane:<id>@cbc-webui", element:<key>}` → 服务端 `pressModSlot` dispatch `ui.press`（e.surface=`cbc-webui`）→ hook 内改 store + `$.ui.invalidate('ui.render')` → SSE 推 `modRenderInvalidate` → 前端重渲染。把逻辑写在 `ui.press` hook 或 `onPress` 里均可。
5. **测试 harness 契约**：被测 Mod 由 `plugin test` 自动加载，测试文件的 `on` 挂在"世界层"（比被测 Mod 更外），必须桩接 Mod 用到的每个 `$` 调用（未桩接的抛错并点名事件）；结果包装不统一——`session.start` 桩回裸结果 `{cwd}`，`ui.open`/`ui.log` 桩要 `{value: ...}`；`mock.store(on, {...})` 顶 `$.store`；面板渲染用 `$.ui.mount({plugin, surface:'desktop', component:'Pane', requestId})`（测试四个面里 desktop 才有 Svg）。
6. **`e.surface` 在 Web 面板渲染时可能报 `desktop`**（snapshot 渲染路径），press 时才是 `cbc-webui`——surface 分支别只写 `=== 'cbc-webui'`。

### 一期起步清单

- 数据：`$.clock.every(1000, ...)` 轮询 `$.mcp.call('adk-dev-flow', 'flow_status')`（需 adk-dev-flow 插件同为已安装状态，MCP 直连无授权弹窗）。
- 渲染：PoC 的 `drawPane` 结构直接复用，把 `buildSvg(count,now)` 换成看板状态位图（工位着色/阶段徽章）。
- 启动集成：`sync_local_plugin.py` 或文档写明两个环境变量；后续可给 `codebuddy --serve` 包一层启动脚本自动带上。
