---
type: ADR
adr: 1
title: '看板接入 CodeBuddy Web UI：采用模组中心常驻整页模组'
status: accepted
date: '2026-11-24'
---

# ADR-0001：看板接入 CodeBuddy Web UI：采用模组中心常驻整页模组

## 背景

[看板集成评估](../dashboard-webui-integration-assessment.md)（2026-11-23）比较了把像素工坊看板送进 CodeBuddy Web UI 的两条官方通道，结论是走 MCP Apps widget（方案 1）。该方案的前提是一次数据层改造：看板不再从独立看板服务的 `/api/*` 取数，改为 MCP 结构化推送加轮询。

## 决策

CodeBuddy Web UI 的看板采用模组中心常驻模组 `mod/pixel-forge-screen`：侧边栏提供整页入口，页面原生重绘看板拓扑。数据仍取自独立看板服务的 `/api/status` 与 `/api/runs`，服务未启动时由模组宿主侧后台拉起。

评估中的方案 1（MCP Apps widget 加数据层改造）和方案 3（`ui.render` 常驻面板）不采用。

## 依据

本 ADR 于 2026-11-26 依据提交历史补录。

- 2026-11-23 `dd4ec93`：`pixel-forge-dock` PoC，验证 dock 面板可渲染动画 SVG。
- 2026-11-24 `155ef03`：加入 `pixel-forge-screen` 模组包。
- 2026-11-24 `f7b42d4`：移除被取代的 `pixel-forge-dock`。
- 截至 2026-11-26，`src/` 中没有 `ext-apps` 引用，方案 1 从未实施。

## 动机

当时选择模组方案的理由没有在仓库中记录。能从证据确认的是：决策保留了独立看板服务及其 SSE 推送，没有执行方案 1 要求的数据层改造。

## 后果

- 独立看板服务（`adk-dev-flow-dashboard`）是大屏的依赖，而不是被大屏替代。
- 大屏每秒轮询 `/api/*`，实时性沿用独立服务。
- 该通道依赖 Web UI 的模组接口，接口变更时模组需要同步修改。
- 评估文档保留为调研记录，状态改为 `superseded`。
