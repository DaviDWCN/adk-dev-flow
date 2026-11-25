import React from 'react';
const plugin=(function(React,host,styles,console){
"use strict";
return {slotContract:'strict',inject:['slots','locale'],apply(ctx){
const ns='pixel-forge-screen';
const h=React.createElement;
ctx.locale.register(ns,{
zh:{title:'像素工坊大屏',sub:'确定性研发流水线监控台',synced:'已连接看板服务',offline:'看板服务未连接',loading:'正在连接看板服务…',noResponse:'无响应（服务未启动）',badHttp:'响应异常',opened:'已请求在浏览器打开：',starting:'正在后台拉起看板服务…',currentRun:'当前任务',refresh:'刷新',openFull:'在浏览器打开完整看板',startServer:'启动看板服务',autoPlay:'▶ 演练流水线',stopAuto:'⏹ 停止演练',backLive:'回到实时',simOn:'演练模式：当前显示的是模拟步骤，不是任务的实时状态。',gates:'核心门禁与审批关卡',lifecycle:'8 步确定性研发全生命周期流水线',contract:'任务契约',terminal:'流水线日志',simDock:'交互式单步仿真控制台',simDockHint:'点击按钮演练各工位流动',attempt:'轮次',noRun:'暂无活跃任务',run:'任务',updated:'更新时间',target:'目标分支',allowed:'白名单',lockedTest:'验收测试',worktree:'施工工位',design:'设计文档',red:'红灯证据',mutation:'变异门禁',preCritic:'前置审查',impact:'AST 影响面',verdict:'质量把关结论',passed:'通过 / PASSED',blocked:'驳回 / BLOCKED',lockNote:'🔒 白名单与测试防篡改双重审计，超范围修改由底层 Git Hook 即时阻断。',art:'像素工坊流水线拓扑图',currentNode:'当前流程节点',activeActor:'执行角色',waitingFor:'当前等待',stationPos:'工位定位'},
en:{title:'Pixel Forge Screen',sub:'Deterministic R&D pipeline monitor',synced:'Dashboard service connected',offline:'Dashboard service is not connected',loading:'Connecting to the dashboard service…',noResponse:'no response (service not started)',badHttp:'unexpected response',opened:'Requested to open in browser: ',starting:'Starting the dashboard service in the background…',currentRun:'Current task',refresh:'Refresh',openFull:'Open full dashboard in browser',startServer:'Start dashboard service',autoPlay:'▶ Run walkthrough',stopAuto:'⏹ Stop walkthrough',backLive:'Back to live',simOn:'Walkthrough mode: showing simulated steps, not the live task state.',gates:'Core gates and approvals',lifecycle:'8-step deterministic lifecycle',contract:'Task contract',terminal:'Pipeline log',simDock:'Interactive step simulator',simDockHint:'Click a step to walk through the stations',attempt:'Attempt',noRun:'No active task',run:'Task',updated:'Updated',target:'Target branch',allowed:'Allowed paths',lockedTest:'Locked test',worktree:'Worktree',design:'Design doc',red:'Red evidence',mutation:'Mutation gate',preCritic:'Pre-critic',impact:'AST impact',verdict:'Critic verdict',passed:'PASSED',blocked:'BLOCKED',lockNote:'🔒 The allowlist and tamper-proof tests are audited; out-of-scope edits are blocked by the Git hook.',art:'Pixel Forge pipeline topology',currentNode:'Current Node',activeActor:'Active Actor',waitingFor:'Waiting For',stationPos:'Station'}});
styles.insert(`.pfs{display:flex;flex-direction:column;gap:12px;max-width:1180px;margin:0 auto;padding:12px;font:inherit;font-size:12px;line-height:1.5;color:var(--color-text-primary);min-height:100%;box-sizing:border-box}
.pfs-full{max-width:none;min-height:100vh}
.pfs *,.pfs *::before,.pfs *::after{box-sizing:border-box}
.pfs h3,.pfs p{margin:0}
.pfs button,.pfs select{font:inherit}
.pfs-card{background:var(--color-bg-card);border:1px solid var(--color-border-muted);border-radius:12px;padding:12px}
.pfs-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.pfs-spread{justify-content:space-between}
.pfs-bar{justify-content:space-between}
.pfs-brand{display:flex;align-items:center;gap:10px}
.pfs-logo{display:flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:10px;background:linear-gradient(135deg,#2563eb,#6366f1);color:#fff;font-weight:900;font-size:14px}
.pfs-title{display:flex;align-items:center;gap:6px;font-weight:700;font-size:13px}
.pfs-tag{padding:0 6px;border:1px solid var(--color-border-muted);border-radius:4px;background:var(--color-bg-chip);color:var(--color-text-secondary);font:10px var(--font-mono,monospace)}
.pfs-sub{font-size:10px;color:var(--color-text-tertiary)}
.pfs-led{width:10px;height:10px;border-radius:50%;background:#9ca3af}
.pfs-led-on{background:#10b981;box-shadow:0 0 8px rgba(16,185,129,.6);animation:pfs-led 1.5s ease-in-out infinite}
.pfs-badge{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid;font:600 11px var(--font-mono,monospace);white-space:nowrap}
.pfs-chip{display:inline-flex;align-items:center;gap:6px;max-width:240px;padding:3px 10px;border:1px solid var(--color-border-muted);border-radius:8px;background:var(--color-bg-input);color:var(--color-text-secondary);font:600 11px var(--font-mono,monospace);white-space:nowrap;overflow:hidden;.pfs-oab{display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid #f59e0b;background:rgba(245,158,11,.08);border-radius:10px;color:#92400e;font-size:11px;flex-wrap:wrap}
.pfs-chip-ma{color:#059669;border-color:rgba(5,150,105,.5);background:rgba(5,150,105,.08)}
.pfs-select{max-width:260px;padding:4px 8px;border:1px solid var(--color-border-input);border-radius:8px;background:var(--color-bg-input);color:var(--color-text-primary);font:11px var(--font-mono,monospace)}
.pfs-btn{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid var(--color-border-default);border-radius:8px;background:var(--color-bg-input);color:var(--color-text-primary);font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap}
.pfs-btn:hover{background:var(--color-bg-hover)}
.pfs-btn:disabled{opacity:.5;cursor:not-allowed}
.pfs-btn:focus-visible,.pfs-pill:focus-visible,.pfs-simbtn:focus-visible,.pfs-select:focus-visible{outline:2px solid var(--color-focus-ring);outline-offset:1px}
.pfs-btn-pri{background:var(--color-accent-blue);border-color:var(--color-accent-blue);color:#fff}
.pfs-btn-indigo{background:#4f46e5;border-color:#4f46e5;color:#fff}
.pfs-btn-rose{background:#e11d48;border-color:#e11d48;color:#fff}
.pfs-btn-warn{border-color:#f59e0b;color:color-mix(in srgb,#b45309 65%,var(--color-text-primary))}
.pfs-banner{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:8px 12px;border-radius:10px;border:1px solid var(--color-border-muted);background:var(--color-info-bg);color:var(--color-text-body);font-size:11px}
.pfs-banner-off{background:var(--color-warning-bg);border-color:rgba(250,173,14,.4)}
.pfs-banner-sim{background:rgba(245,158,11,.14);border-color:rgba(245,158,11,.45);color:color-mix(in srgb,#b45309 65%,var(--color-text-primary))}
.pfs-err{font:11px var(--font-mono,monospace);color:var(--color-accent-red);word-break:break-all}
.pfs-gates-title{font-weight:700;margin-right:4px}
.pfs-gate{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:8px;border:1px solid;font:10px var(--font-mono,monospace);opacity:.55;transition:opacity .2s,box-shadow .2s}
.pfs-gate-on{opacity:1;box-shadow:0 0 0 2px rgba(22,119,255,.18)}
.pfs-sec-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;font-weight:700}
.pfs-steps{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px}
.pfs-pill{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px;border:1px solid var(--color-border-muted);border-radius:12px;background:var(--color-bg-input);color:var(--color-text-body);font:700 10px var(--font-mono,monospace);text-align:center;cursor:pointer;transition:transform .2s,opacity .2s,border-color .2s}
.pfs-pill:hover{opacity:1;border-color:var(--color-accent-blue)}
.pfs-pill-todo{opacity:.65}
.pfs-pill-done{border-color:#6ee7b7;background:var(--color-success-bg);opacity:.9}
.pfs-pill-on{border:2px solid var(--color-accent-blue);background:var(--color-info-bg);transform:scale(1.04)}
.pfs-num{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;margin-right:4px;border-radius:50%;font-size:9px}
.pfs-pill-sub{font-size:9px;font-weight:500;color:var(--color-text-tertiary)}
.pfs-canvas{padding:8px}
.pfs-canvas svg{width:100%;height:auto;display:block}
.pfs-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.pfs-panel{display:flex;flex-direction:column;gap:10px}
.pfs-h3{font-size:12px;font-weight:700}
.pfs-kv{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:2px 0;font:11px var(--font-mono,monospace)}
.pfs-k{color:var(--color-text-tertiary);white-space:nowrap}
.pfs-v{max-width:70%;padding:1px 8px;border:1px solid var(--color-border-muted);border-radius:6px;background:var(--color-bg-input);color:var(--color-text-body);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pfs-review,.pfs-lock{padding:10px;border:1px solid var(--color-border-muted);border-radius:10px;background:var(--color-bg-input);font:11px var(--font-mono,monospace);color:var(--color-text-body)}
.pfs-review{display:flex;flex-direction:column;gap:6px}
.pfs-issues{padding-top:6px;border-top:1px solid rgba(244,63,94,.25);color:color-mix(in srgb,#e11d48 70%,var(--color-text-primary))}
.pfs-muted{font-size:9px;color:var(--color-text-tertiary)}
.pfs-file{font-weight:700}
.pfs-dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px}
.pfs-term{display:flex;flex-direction:column;gap:6px;min-height:90px;padding:12px;border:1px solid #1e293b;border-radius:12px;background:#020617;color:#e2e8f0;font:11px var(--font-mono,monospace)}
.pfs-term-p{display:flex;gap:6px;font-weight:600;color:#34d399;word-break:break-word}
.pfs-prompt{color:#64748b}
.pfs-term-s{padding-left:14px;color:#94a3b8;word-break:break-word}
.pfs-simhead{font-size:10px}
.pfs-simg{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:6px}
.pfs-simbtn{padding:6px 2px;border:1px solid;border-radius:8px;background:var(--color-bg-card);font:700 9px var(--font-mono,monospace);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pfs-simbtn-on{outline:2px solid var(--color-focus-ring)}
.pfs-t-slate{background:#f1f5f9;color:#64748b;border-color:#cbd5e1}
.pfs-t-blue{background:rgba(59,130,246,.14);color:color-mix(in srgb,#2563eb 65%,var(--color-text-primary));border-color:rgba(59,130,246,.4)}
.pfs-t-amber{background:rgba(245,158,11,.16);color:color-mix(in srgb,#b45309 65%,var(--color-text-primary));border-color:rgba(245,158,11,.45)}
.pfs-t-orange{background:rgba(249,115,23,.14);color:color-mix(in srgb,#c2410c 65%,var(--color-text-primary));border-color:rgba(249,115,23,.4)}
.pfs-t-purple{background:rgba(139,92,246,.14);color:color-mix(in srgb,#6d28d9 65%,var(--color-text-primary));border-color:rgba(139,92,246,.4)}
.pfs-t-cyan{background:rgba(6,182,212,.14);color:color-mix(in srgb,#0e7490 65%,var(--color-text-primary));border-color:rgba(6,182,212,.4)}
.pfs-t-emerald{background:rgba(16,185,129,.14);color:color-mix(in srgb,#047857 65%,var(--color-text-primary));border-color:rgba(16,185,129,.4)}
.pfs-t-rose{background:rgba(244,63,94,.14);color:color-mix(in srgb,#be123c 65%,var(--color-text-primary));border-color:rgba(244,63,94,.4)}
@keyframes pfs-hammer-swing{0%,100%{transform:rotate(0)}50%{transform:rotate(-25deg)}}
@keyframes pfs-tube-shake{0%,100%{transform:rotate(0)}25%{transform:rotate(10deg)}75%{transform:rotate(-8deg)}}
@keyframes pfs-glass-scan{0%,100%{transform:translate(0,0) rotate(0)}50%{transform:translate(-2px,-2px) rotate(-12deg)}}
@keyframes pfs-truck-vibe{0%,100%{transform:translateY(0)}50%{transform:translateY(-1.5px)}}
@keyframes pfs-spin{to{transform:rotate(360deg)}}
@keyframes pfs-scan{0%{transform:translateY(-4px);opacity:0}50%{opacity:.9}100%{transform:translateY(10px);opacity:0}}
@keyframes pfs-flow{from{stroke-dashoffset:24}to{stroke-dashoffset:0}}
@keyframes pfs-flow-rev{from{stroke-dashoffset:0}to{stroke-dashoffset:24}}
@keyframes pfs-glow{0%,100%{filter:drop-shadow(0 0 4px rgba(56,189,248,.4))}50%{filter:drop-shadow(0 0 10px rgba(56,189,248,.8))}}
@keyframes pfs-led{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.9)}}
.pfs-anim-hammer{transform-box:fill-box;transform-origin:0 100%;animation:pfs-hammer-swing .5s ease-in-out infinite}
.pfs-anim-tube{transform-box:fill-box;transform-origin:50% 100%;animation:pfs-tube-shake .7s ease-in-out infinite}
.pfs-anim-glass{transform-box:fill-box;transform-origin:50% 50%;animation:pfs-glass-scan 1.2s ease-in-out infinite}
.pfs-anim-truck{transform-box:fill-box;transform-origin:50% 100%;animation:pfs-truck-vibe .35s ease-in-out infinite}
.pfs-anim-spin{transform-box:fill-box;transform-origin:50% 50%;animation:pfs-spin 1.8s linear infinite}
.pfs-scan{transform-box:fill-box;animation:pfs-scan 1.2s ease-in-out infinite}
.pfs-pipe{stroke-dasharray:6 6;animation:pfs-flow .8s linear infinite}
.pfs-pipe-rev{stroke-dasharray:6 6;animation:pfs-flow-rev .8s linear infinite}
.pfs-node-active{animation:pfs-glow 2s ease-in-out infinite}
.pfs-avatar{transform-box:fill-box;transform-origin:50% 50%;transition:transform .3s,filter .3s}
.pfs-hero{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-radius:12px;border:1px solid var(--color-border-muted);background:linear-gradient(135deg,rgba(59,130,246,0.06),rgba(99,102,241,0.08));box-shadow:0 2px 8px -2px rgba(59,130,246,0.08)}
.pfs-hero-main{display:flex;align-items:center;gap:12px}
.pfs-hero-beacon{position:relative;display:flex;align-items:center;justify-content:center;width:14px;height:14px}
.pfs-hero-ping{position:absolute;width:14px;height:14px;border-radius:50%;opacity:0.75;animation:pfs-led 1.5s ease-in-out infinite}
.pfs-hero-dot{position:relative;width:12px;height:12px;border-radius:50%}
.pfs-hero-content{display:flex;flex-direction:column;gap:3px}
.pfs-hero-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.pfs-hero-lbl{font-size:10px;font-weight:800;text-transform:uppercase;color:var(--color-text-tertiary)}
.pfs-hero-stage{font:700 11px var(--font-mono,monospace);padding:2px 8px;border-radius:999px;border:1px solid}
.pfs-hero-actor{font:600 11px var(--font-mono,monospace);padding:2px 8px;border-radius:6px;background:var(--color-bg-input);border:1px solid var(--color-border-muted);color:var(--color-text-secondary)}
.pfs-hero-action{font-size:12px;font-weight:600;color:var(--color-text-primary)}
.pfs-hero-side{display:flex;align-items:center;gap:8px;font:11px var(--font-mono,monospace)}
.pfs-hero-box{display:flex;align-items:center;gap:6px;padding:4px 10px;border-radius:8px;background:var(--color-bg-input);border:1px solid var(--color-border-muted)}
.pfs-hero-box-lbl{color:var(--color-text-tertiary)}
.pfs-pill-active-sub{color:var(--color-accent-blue);font-weight:700}
@media (max-width:900px){.pfs-steps{grid-template-columns:repeat(4,minmax(0,1fr))}.pfs-cols{grid-template-columns:minmax(0,1fr)}.pfs-simg{grid-template-columns:repeat(5,minmax(0,1fr))}}
@media (prefers-reduced-motion:reduce){.pfs *{transition:none!important;animation:none!important}}`);
// 拓扑图素材：取自看板页面 templates/dashboard.html 的 #topology-svg，运行时解析成 React 元素（id 统一加 pfs- 前缀）
const ART=`<svg viewBox="0 0 960 380">
<defs>
<pattern id="blueprint-dots" width="16" height="16" patternUnits="userSpaceOnUse">
<circle cx="2" cy="2" r="0.8" fill="#cbd5e1" opacity="0.4"/>
</pattern>
<filter id="soft-shadow" x="-5%" y="-5%" width="110%" height="115%">
<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0f172a" flood-opacity="0.06"/>
</filter>
<filter id="pipe-glow" x="-20%" y="-20%" width="140%" height="140%">
<feGaussianBlur stdDeviation="2.5" result="blur" />
<feMerge>
<feMergeNode in="blur"/>
<feMergeNode in="SourceGraphic"/>
</feMerge>
</filter>
<marker id="arrow-gray" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#94a3b8" />
</marker>
<marker id="arrow" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#3b82f6" />
</marker>
<marker id="arrow-green" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#10b981" />
</marker>
<marker id="arrow-amber" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#f59e0b" />
</marker>
<marker id="arrow-rose" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#ef4444" />
</marker>
<marker id="arrow-cyan" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#06b6d4" />
</marker>
<marker id="arrow-purple" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto">
<path d="M 0 1 L 8 5 L 0 9 z" fill="#8b5cf6" />
</marker>
</defs>
<rect x="0" y="0" width="960" height="380" fill="url(#blueprint-dots)" />
<rect x="15" y="24" width="930" height="138" rx="14" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.2" stroke-dasharray="4,4" filter="url(#soft-shadow)" />
<g transform="translate(30, 15)">
<rect x="0" y="0" width="195" height="20" rx="8" fill="#f8fafc" stroke="#cbd5e1" stroke-width="0.8"/>
<circle cx="10" cy="10" r="3" fill="#3b82f6"/>
<text x="105" y="13.5" text-anchor="middle" font-size="8.5" font-weight="700" fill="#334155">上轨 · 契约蓝图与认知审查域 (步骤 1-4)</text>
</g>
<rect x="15" y="210" width="930" height="158" rx="14" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.2" filter="url(#soft-shadow)" />
<g transform="translate(30, 201)">
<rect x="0" y="0" width="195" height="20" rx="8" fill="#f8fafc" stroke="#cbd5e1" stroke-width="0.8"/>
<circle cx="10" cy="10" r="3" fill="#8b5cf6"/>
<text x="105" y="13.5" text-anchor="middle" font-size="8.5" font-weight="700" fill="#334155">下轨 · 隔离施工跑道与成果交付域 (步骤 5-8)</text>
</g>
<g id="flow-conduits">
<path d="M 237 95 L 262 95" fill="none" stroke="#cbd5e1" stroke-width="2.5" marker-end="url(#arrow-gray)" />
<path id="pipe-1-2" d="M 237 95 L 262 95" fill="none" stroke="#3b82f6" stroke-width="2.8" class="pipe-active hidden" marker-end="url(#arrow)" filter="url(#pipe-glow)" />
<path d="M 467 95 L 492 95" fill="none" stroke="#cbd5e1" stroke-width="2.5" marker-end="url(#arrow-gray)" />
<path id="pipe-2-3" d="M 467 95 L 492 95" fill="none" stroke="#10b981" stroke-width="2.8" class="pipe-active hidden" marker-end="url(#arrow-green)" filter="url(#pipe-glow)" />
<path d="M 697 95 L 722 95" fill="none" stroke="#cbd5e1" stroke-width="2.5" marker-end="url(#arrow-gray)" />
<path id="pipe-3-4" d="M 697 95 L 722 95" fill="none" stroke="#06b6d4" stroke-width="2.8" class="pipe-active hidden" marker-end="url(#arrow-cyan)" filter="url(#pipe-glow)" />
<path d="M 824.5 154 L 824.5 188 L 134.5 188 L 134.5 222" fill="none" stroke="#cbd5e1" stroke-width="2.5" marker-end="url(#arrow-gray)" />
<path id="pipe-4-5" d="M 824.5 154 L 824.5 188 L 134.5 188 L 134.5 222" fill="none" stroke="#f59e0b" stroke-width="3" class="pipe-active hidden" marker-end="url(#arrow-amber)" filter="url(#pipe-glow)" />
<path d="M 237 290 L 262 290" fill="none" stroke="#cbd5e1" stroke-width="2.5" marker-end="url(#arrow-gray)" />
<path id="pipe-5-6" d="M 237 290 L 262 290" fill="none" stroke="#8b5cf6" stroke-width="2.8" class="pipe-active hidden" marker-end="url(#arrow-purple)" filter="url(#pipe-glow)" />
<path d="M 364.5 222 C 364.5 170, 134.5 170, 134.5 222" fill="none" stroke="#f59e0b" stroke-width="2" stroke-dasharray="4,4" marker-end="url(#arrow-amber)" />
<path id="pipe-loop-tdd" d="M 364.5 222 C 364.5 170, 134.5 170, 134.5 222" fill="none" stroke="#f59e0b" stroke-width="2.8" class="pipe-loopback hidden" marker-end="url(#arrow-amber)" filter="url(#pipe-glow)" />
<path d="M 467 290 L 492 290" fill="none" stroke="#ef4444" stroke-width="2" stroke-dasharray="4,4" marker-end="url(#arrow-rose)" />
<path id="pipe-branch-escalate" d="M 467 290 L 492 290" fill="none" stroke="#ef4444" stroke-width="3" class="pipe-active hidden" marker-end="url(#arrow-rose)" filter="url(#pipe-glow)" />
<path d="M 467 312 L 722 312" fill="none" stroke="#cbd5e1" stroke-width="2.5" marker-end="url(#arrow-gray)" />
<path id="pipe-6-8" d="M 467 312 L 722 312" fill="none" stroke="#10b981" stroke-width="3" class="pipe-active hidden" marker-end="url(#arrow-green)" filter="url(#pipe-glow)" />
</g>
<g id="conduit-badges">
<g transform="translate(225, 87)">
<rect x="0" y="0" width="50" height="16" rx="6" fill="#ffffff" stroke="#3b82f6" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="25" y="11" text-anchor="middle" font-size="7" font-weight="bold" fill="#1d4ed8">蓝图 ➔</text>
</g>
<g transform="translate(455, 87)">
<rect x="0" y="0" width="50" height="16" rx="6" fill="#ffffff" stroke="#10b981" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="25" y="11" text-anchor="middle" font-size="7" font-weight="bold" fill="#047857">用例 ➔</text>
</g>
<g transform="translate(685, 87)">
<rect x="0" y="0" width="50" height="16" rx="6" fill="#ffffff" stroke="#06b6d4" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="25" y="11" text-anchor="middle" font-size="7" font-weight="bold" fill="#0e7490">双审 ➔</text>
</g>
<g transform="translate(385, 179)">
<rect x="0" y="0" width="190" height="18" rx="8" fill="#fffbeb" stroke="#f59e0b" stroke-width="1.2" filter="url(#soft-shadow)"/>
<text x="95" y="12.5" text-anchor="middle" font-size="8" font-weight="bold" fill="#b45309">✍️ 契约签字锁定 ➔ 准入沙箱施工</text>
</g>
<g transform="translate(225, 282)">
<rect x="0" y="0" width="50" height="16" rx="6" fill="#ffffff" stroke="#8b5cf6" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="25" y="11" text-anchor="middle" font-size="7" font-weight="bold" fill="#6d28d9">送测 ➔</text>
</g>
<g transform="translate(195, 160)">
<rect x="0" y="0" width="110" height="17" rx="7" fill="#fff7ed" stroke="#f97316" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="55" y="12" text-anchor="middle" font-size="7" font-weight="bold" fill="#c2410c">🔄 7. 自愈打回 (≤3次)</text>
</g>
<g transform="translate(455, 282)">
<rect x="0" y="0" width="50" height="16" rx="6" fill="#fff1f2" stroke="#ef4444" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="25" y="11" text-anchor="middle" font-size="7" font-weight="bold" fill="#ef4444">🚨 熔断</text>
</g>
<g transform="translate(530, 303)">
<rect x="0" y="0" width="130" height="18" rx="7" fill="#f0fdf4" stroke="#10b981" stroke-width="1" filter="url(#soft-shadow)"/>
<text x="65" y="12.5" text-anchor="middle" font-size="7.5" font-weight="bold" fill="#047857">✅ 跑测全绿 ➔ AST门禁审查</text>
</g>
</g>
<g id="node-spec" onclick="setStep(1)">
<title>① 架构设计 AI：出架构设计文档与原子任务拆分</title>
<rect x="32" y="36" width="205" height="118" rx="10" fill="#ffffff" stroke="#93c5fd" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="32" y="36" width="205" height="28" rx="10" fill="#eff6ff" />
<rect x="32" y="54" width="205" height="10" fill="#eff6ff" />
<circle cx="48" cy="50" r="3" fill="#3b82f6"/>
<text x="134.5" y="53.5" text-anchor="middle" font-size="9" font-weight="700" fill="#1d4ed8">1. 设计 AI 📐 方案蓝图</text>
<rect x="95" y="104" width="78" height="5" rx="2" fill="#e2e8f0"/>
<g transform="translate(114, 68)">
<g id="svg-architect">
<ellipse cx="20" cy="11" rx="9" ry="5" fill="#3b82f6"/>
<path d="M12 11 Q20 4 28 11" fill="#2563eb"/>
<circle cx="20" cy="17" r="7" fill="#fde047"/>
<circle cx="18" cy="16" r="1.2" fill="#1e293b"/>
<circle cx="22" cy="16" r="1.2" fill="#1e293b"/>
<rect x="13" y="24" width="14" height="15" rx="3" fill="#1e40af"/>
<rect x="23" y="24" width="4" height="12" rx="1" fill="#e0f2fe" transform="rotate(25 23 24)"/>
</g>
</g>
<rect x="42" y="126" width="185" height="18" rx="6" fill="#f8fafc" stroke="#e2e8f0" stroke-width="0.8"/>
<text x="134.5" y="138" text-anchor="middle" font-size="7.5" font-weight="600" fill="#475569">产出设计文档 & 任务拆分</text>
</g>
<g id="node-step-2" onclick="setStep(2)">
<title>② 验收测试 AI：出红灯测试用例，真实预检断言失败，并通过 AST 变异验真</title>
<rect x="262" y="36" width="205" height="118" rx="10" fill="#ffffff" stroke="#86efac" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="262" y="36" width="205" height="28" rx="10" fill="#f0fdf4" />
<rect x="262" y="54" width="205" height="10" fill="#f0fdf4" />
<circle cx="278" cy="50" r="3" fill="#10b981"/>
<text x="364.5" y="53.5" text-anchor="middle" font-size="9" font-weight="700" fill="#15803d">2. 测试 AI 🧪 红灯用例</text>
<rect x="325" y="104" width="78" height="5" rx="2" fill="#e2e8f0"/>
<g transform="translate(344, 68)">
<g id="svg-tester">
<ellipse cx="20" cy="10" rx="8" ry="4" fill="#10b981"/>
<circle cx="20" cy="16" r="6.5" fill="#fed7aa"/>
<rect x="15" y="13" width="10" height="4" rx="2" fill="#0284c7" opacity="0.8"/>
<rect x="13" y="22" width="14" height="15" rx="2" fill="#f8fafc"/>
<g transform="translate(28, 23)">
<rect id="svg-tube" x="-2" y="-5" width="3.5" height="11" rx="1.5" fill="#10b981" />
</g>
</g>
</g>
<rect x="272" y="126" width="185" height="18" rx="6" fill="#f8fafc" stroke="#e2e8f0" stroke-width="0.8"/>
<text x="364.5" y="138" text-anchor="middle" font-size="7.5" font-weight="600" fill="#475569">红灯预检 · AST 变异验真</text>
</g>
<g id="node-critic" onclick="setStep(3)">
<title>③ 前置审查：随契约起草内嵌执行，评审设计与测试一致性及架构规则</title>
<rect x="492" y="36" width="205" height="118" rx="10" fill="#ffffff" stroke="#67e8f9" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="492" y="36" width="205" height="28" rx="10" fill="#ecfeff" />
<rect x="492" y="54" width="205" height="10" fill="#ecfeff" />
<circle cx="508" cy="50" r="3" fill="#06b6d4"/>
<text x="594.5" y="53.5" text-anchor="middle" font-size="9" font-weight="700" fill="#0891b2">3. Critic 双审 🧐 规范对齐</text>
<rect x="555" y="104" width="78" height="5" rx="2" fill="#e2e8f0"/>
<g transform="translate(574, 68)">
<g id="svg-critic">
<ellipse cx="20" cy="9" rx="10" ry="3" fill="#475569"/>
<circle cx="20" cy="15" r="6.5" fill="#fed7aa"/>
<circle cx="18" cy="15" r="3" stroke="#f59e0b" stroke-width="1" fill="none"/>
<rect x="13" y="22" width="14" height="15" rx="2" fill="#334155"/>
<g transform="translate(27, 20)">
<g id="svg-glass">
<circle cx="0" cy="0" r="4.5" stroke="#f59e0b" stroke-width="2" fill="none"/>
<line x1="3" y1="3" x2="8" y2="8" stroke="#d97706" stroke-width="2"/>
</g>
</g>
<polygon id="svg-critic-beam" points="0,0 -8,18 8,18" fill="#f59e0b" opacity="0.25" class="animate-scan" transform="translate(27, 20)"/>
</g>
</g>
<rect x="502" y="126" width="185" height="18" rx="6" fill="#f8fafc" stroke="#e2e8f0" stroke-width="0.8"/>
<text x="594.5" y="138" text-anchor="middle" font-size="7.5" font-weight="600" fill="#475569">设计与用例一致 · 架构防破坏</text>
</g>
<g id="badge-hitl-spec" onclick="setStep(4)">
<title>④ 关键 HITL：人工签字确认任务和测试 cases，锁定契约规范</title>
<rect x="722" y="36" width="205" height="118" rx="10" fill="#ffffff" stroke="#fcd34d" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="722" y="36" width="205" height="28" rx="10" fill="#fffbeb" />
<rect x="722" y="54" width="205" height="10" fill="#fffbeb" />
<circle cx="738" cy="50" r="3" fill="#f59e0b"/>
<text x="824.5" y="53.5" text-anchor="middle" font-size="9" font-weight="700" fill="#b45309">4. 关键 HITL ✍️ 签字审批</text>
<g transform="translate(774, 68)">
<rect x="0" y="0" width="100" height="46" rx="6" fill="#fefce8" stroke="#fde047" stroke-width="1.2"/>
<line x1="18" y1="12" x2="52" y2="12" stroke="#d97706" stroke-width="1.8" stroke-linecap="round"/>
<rect x="14" y="18" width="72" height="20" rx="5" fill="#fef3c7" stroke="#b45309" stroke-width="1.2"/>
<text x="50" y="31.5" text-anchor="middle" font-size="8.5" font-weight="bold" fill="#b45309">✍️ 契约已签字锁定</text>
</g>
<rect x="732" y="126" width="185" height="18" rx="6" fill="#fffbeb" stroke="#fde68a" stroke-width="0.8"/>
<text x="824.5" y="138" text-anchor="middle" font-size="7.5" font-weight="700" fill="#92400e">人工审核确认 · 冻结准入</text>
</g>
<g id="node-work" onclick="setStep(5)">
<title>⑤ 开发 AI：AI 工兵在受限独立沙箱内编码，以通过全部红灯用例为目标</title>
<rect x="32" y="222" width="205" height="136" rx="10" fill="#ffffff" stroke="#fcd34d" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="32" y="222" width="205" height="28" rx="10" fill="#fffbeb" />
<rect x="32" y="240" width="205" height="10" fill="#fffbeb" />
<circle cx="48" cy="236" r="3" fill="#f59e0b"/>
<text x="134.5" y="239.5" text-anchor="middle" font-size="9" font-weight="700" fill="#b45309">5. 开发 AI 🔨 沙箱编码</text>
<rect x="95" y="296" width="78" height="5" rx="2" fill="#e2e8f0"/>
<g transform="translate(114, 256)">
<g id="svg-builder">
<ellipse cx="22" cy="10" rx="9" ry="4" fill="#fbbf24"/>
<path d="M14 10 Q22 4 30 10" fill="#f59e0b"/>
<circle cx="22" cy="16" r="6.5" fill="#fed7aa"/>
<circle cx="20" cy="15" r="1" fill="#334155"/>
<circle cx="24" cy="15" r="1" fill="#334155"/>
<rect x="15" y="22" width="14" height="14" rx="2" fill="#ea580c"/>
<rect x="17" y="24" width="2" height="12" fill="#fef08a"/>
<rect x="25" y="24" width="2" height="12" fill="#fef08a"/>
<g transform="translate(28, 23)">
<g id="svg-hammer">
<line x1="0" y1="0" x2="8" y2="-5" stroke="#fed7aa" stroke-width="3" stroke-linecap="round"/>
<line x1="6" y1="-3" x2="16" y2="-10" stroke="#78350f" stroke-width="2.5" stroke-linecap="round"/>
<rect x="13" y="-14" width="8" height="6" rx="1.5" fill="#64748b"/>
</g>
</g>
</g>
</g>
<rect x="42" y="332" width="185" height="18" rx="6" fill="#fefce8" stroke="#fef08a" stroke-width="0.8"/>
<text x="134.5" y="344" text-anchor="middle" font-size="7.5" font-weight="600" fill="#a16207">受限沙箱 Worktree · 安全编码</text>
</g>
<g id="node-test" onclick="setStep(6)">
<title>⑥ 自动化测试跑道：纯机械化执行，核验测试防篡改 + 全量用例真机跑测</title>
<rect x="262" y="222" width="205" height="136" rx="10" fill="#ffffff" stroke="#c4b5fd" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="262" y="222" width="205" height="28" rx="10" fill="#faf5ff" />
<rect x="262" y="240" width="205" height="10" fill="#faf5ff" />
<circle cx="278" cy="236" r="3" fill="#8b5cf6"/>
<text x="364.5" y="239.5" text-anchor="middle" font-size="9" font-weight="700" fill="#6d28d9">6. 自动化测试 ⚙️ 真机跑测</text>
<g transform="translate(284, 256)">
<rect x="0" y="0" width="160" height="58" rx="6" fill="#0f172a" stroke="#1e293b" stroke-width="1"/>
<circle cx="8" cy="8" r="2.2" fill="#ef4444"/>
<circle cx="15" cy="8" r="2.2" fill="#f59e0b"/>
<circle cx="22" cy="8" r="2.2" fill="#10b981"/>
<text x="8" y="24" font-size="7.5" fill="#94a3b8">$ pytest -v --tb=short</text>
<rect x="6" y="32" width="148" height="20" rx="4" fill="#1e293b"/>
<text x="12" y="46" font-size="8" font-weight="bold" fill="#10b981">✓ PASSED (100% GREEN)</text>
<g id="svg-runner-gears" transform="translate(138, 42)">
<circle cx="0" cy="0" r="5" stroke="#8b5cf6" stroke-width="1.2" fill="none" stroke-dasharray="2,2"/>
<circle cx="0" cy="0" r="1.5" fill="#8b5cf6"/>
</g>
</g>
<rect x="272" y="332" width="185" height="18" rx="6" fill="#f5f3ff" stroke="#ddd6fe" stroke-width="0.8"/>
<text x="364.5" y="344" text-anchor="middle" font-size="7.5" font-weight="600" fill="#7c3aed">防篡改 · lint · 验收跑测</text>
</g>
<g id="node-escalate" onclick="setStep(7)">
<title>⑦ 自愈打回 / 异常跳闸：提取错误指纹打回重试 (≤3次)；连续超限自动熔断</title>
<rect x="492" y="222" width="205" height="136" rx="10" fill="#ffffff" stroke="#fca5a5" stroke-width="1.4" filter="url(#soft-shadow)" />
<rect x="492" y="222" width="205" height="28" rx="10" fill="#fff1f2" />
<rect x="492" y="240" width="205" height="10" fill="#fff1f2" />
<circle cx="508" cy="236" r="3" fill="#ef4444"/>
<text x="594.5" y="239.5" text-anchor="middle" font-size="9" font-weight="700" fill="#e11d48">7. 自愈打回 🔄 / 跳闸 🚨</text>
<g id="svg-siren" transform="translate(574, 254)">
<rect x="10" y="24" width="20" height="8" rx="2" fill="#334155"/>
<line x1="12" y1="32" x2="16" y2="24" stroke="#fbbf24" stroke-width="1.5"/>
<line x1="20" y1="32" x2="24" y2="24" stroke="#fbbf24" stroke-width="1.5"/>
<path d="M14 24 C14 15, 26 15, 26 24 Z" fill="#ef4444"/>
<g transform="translate(20, 18)">
<g id="svg-siren-rays" class="hidden">
<line x1="0" y1="-6" x2="0" y2="-14" stroke="#ef4444" stroke-width="2"/>
<line x1="-8" y1="0" x2="-15" y2="-3" stroke="#ef4444" stroke-width="2"/>
<line x1="8" y1="0" x2="15" y2="-3" stroke="#ef4444" stroke-width="2"/>
</g>
</g>
</g>
<rect x="502" y="296" width="185" height="18" rx="6" fill="#fff7ed" stroke="#fdba74" stroke-width="0.8"/>
<text x="594.5" y="308.5" text-anchor="middle" font-size="7.5" font-weight="bold" fill="#c2410c">🔄 指纹自愈回流 (≤3次)</text>
<rect x="502" y="332" width="185" height="18" rx="6" fill="#fef2f2" stroke="#fecaca" stroke-width="0.8"/>
<text x="594.5" y="344" text-anchor="middle" font-size="7.5" font-weight="700" fill="#dc2626">超限物理熔断 · 人工介入复位</text>
</g>
<g id="node-mr" onclick="setStep(8)">
<title>⑧ 成果交付：AST 语义硬门禁一票否决核验，成果安全合入代码库并交付</title>
<rect x="722" y="222" width="205" height="136" rx="10" fill="#ffffff" stroke="#6ee7b7" stroke-width="1.4" filter="url(#soft-shadow)" />
<g id="badge-gate-evidence">
<rect x="722" y="222" width="205" height="28" rx="10" fill="#ecfdf5" />
<rect x="722" y="240" width="205" height="10" fill="#ecfdf5" />
<circle cx="738" cy="236" r="3" fill="#10b981"/>
<text x="824.5" y="239.5" text-anchor="middle" font-size="9" font-weight="700" fill="#059669">8. 成果交付 🚀 仓库合入</text>
</g>
<rect x="770" y="298" width="108" height="4" rx="2" fill="#94a3b8"/>
<g transform="translate(784, 258)">
<g id="svg-truck">
<rect x="6" y="14" width="16" height="16" rx="3" fill="#6366f1"/>
<rect x="8" y="16" width="6" height="6" rx="1" fill="#c7d2fe"/>
<rect x="22" y="19" width="30" height="11" rx="1" fill="#4338ca"/>
<rect x="25" y="9" width="24" height="11" rx="2" fill="#fbbf24" stroke="#d97706" stroke-width="1"/>
<circle cx="12" cy="30" r="4" fill="#1e293b"/>
<circle cx="44" cy="30" r="4" fill="#1e293b"/>
</g>
</g>
<rect x="732" y="302" width="185" height="18" rx="6" fill="#f0fdf4" stroke="#86efac" stroke-width="0.8"/>
<text x="824.5" y="314.5" text-anchor="middle" font-size="7.5" font-weight="bold" fill="#15803d">🛡️ AST 语义门禁一票否决</text>
<rect x="732" y="332" width="185" height="18" rx="6" fill="#f0fdf4" stroke="#bbf7d0" stroke-width="0.8"/>
<text x="824.5" y="344" text-anchor="middle" font-size="7.5" font-weight="600" fill="#15803d">MR 自动创建 · 安全合入主干</text>
</g>
</svg>`;
const TAGS={svg:1,defs:1,pattern:1,filter:1,feDropShadow:1,feGaussianBlur:1,feMerge:1,feMergeNode:1,marker:1,path:1,rect:1,g:1,text:1,title:1,circle:1,ellipse:1,line:1,polygon:1};
const ATTR={'stroke-width':'strokeWidth','stroke-dasharray':'strokeDasharray','stroke-linecap':'strokeLinecap','flood-color':'floodColor','flood-opacity':'floodOpacity','font-size':'fontSize','font-weight':'fontWeight','text-anchor':'textAnchor','marker-end':'markerEnd'};
const parseSvg=src=>{const root={tag:'root',attrs:{},children:[]},stack=[root],re=/<\/([\w:-]+)\s*>|<([\w:-]+)((?:\s+[\w:-]+="[^"]*")*)\s*(\/?)>|([^<]+)/g;let m;while((m=re.exec(src))){if(m[1]){stack.pop();}else if(m[2]){const attrs={};m[3].replace(/([\w:-]+)="([^"]*)"/g,(_,k,v)=>{attrs[k]=v;});const node={tag:m[2],attrs,children:[]};stack[stack.length-1].children.push(node);if(!m[4])stack.push(node);}else if(stack.length>1&&m[5].trim()){stack[stack.length-1].children.push({text:m[5]});}}return root.children[0];};
const ROOT=parseSvg(ART);
const byId={};
(function walk(n){if(n.attrs&&n.attrs.id)byId[n.attrs.id]=n;(n.children||[]).forEach(walk);})(ROOT);
const findRect=n=>{for(const c of n.children){if(c.tag==='rect')return c;if(c.tag){const r=findRect(c);if(r)return r;}}return null;};
// 节点默认边框色、随状态显隐的元素、带动画的元素（与看板页面 setStep/setStage 一致）
const NODE_STROKE={'node-spec':'#93c5fd','node-step-2':'#86efac','node-critic':'#67e8f9','badge-hitl-spec':'#fcd34d','node-work':'#fcd34d','node-test':'#c4b5fd','node-escalate':'#fca5a5','node-mr':'#6ee7b7'};
const HIDDEN={'pipe-1-2':1,'pipe-2-3':1,'pipe-3-4':1,'pipe-4-5':1,'pipe-5-6':1,'pipe-loop-tdd':1,'pipe-branch-escalate':1,'pipe-6-8':1,'svg-siren-rays':1};
const ANIM={'svg-hammer':'hammer','svg-tube':'tube','svg-glass':'glass','svg-truck':'truck','svg-runner-gears':'spin','svg-siren-rays':'spin','svg-critic-beam':'scan'};
const FIRST=new Map();
Object.keys(NODE_STROKE).forEach(id=>{const n=byId[id];const r=n&&findRect(n);if(r)FIRST.set(r,id);});
const render=(node,st,key)=>{
if(node.text!=null)return node.text;
if(!TAGS[node.tag])return null;
const a=node.attrs,id=a.id,cls=(a.class||'').split(/\s+/),p={key};
for(const k in a){if(k==='class'||k==='onclick'||k==='id')continue;const v=a[k];if(k==='marker-end')p.markerEnd=v.replace('url(#','url(#pfs-');else if(v.indexOf('url(#')>=0)p[k]=v.replace(/url\(#/g,'url(#pfs-');else p[ATTR[k]||k]=v;}
if(id)p.id='pfs-'+id;
const cl=[];
if(cls.indexOf('pipe-active')>=0)cl.push('pfs-pipe');
if(cls.indexOf('pipe-loopback')>=0)cl.push('pfs-pipe-rev');
if(cls.indexOf('animate-scan')>=0)cl.push('pfs-scan');
if(ANIM[id]&&st.anim[id])cl.push('pfs-anim-'+st.anim[id]);
if(st.nodes[id])cl.push('pfs-node-active');
if(HIDDEN[id])p.style={display:st.visible[id]?'':'none'};
if(st.avatar===id){p.style=Object.assign({},p.style,{filter:'drop-shadow(0 0 6px '+st.avatarColor+')',transform:'scale(1.08)'});cl.push('pfs-avatar');}
const nid=FIRST.get(node);
if(nid){p.stroke=st.nodes[nid]||NODE_STROKE[nid];p.strokeWidth=st.nodes[nid]?2.8:1.4;}
const hit=/setStep\((\d+)\)/.exec(a.onclick||'');
if(hit){p.style=Object.assign({},p.style,{cursor:'pointer'});p.onClick=()=>st.onStep(Number(hit[1]));}
if(node.tag==='svg'){p.style={width:'100%',height:'auto',display:'block'};p.role='img';p['aria-label']=ctx.locale.t(ns,'art');}
if(cl.length)p.className=cl.join(' ');
const el=React.createElement(node.tag,p,node.children.map((c,i)=>render(c,st,i)));
// 带 transform 属性又要做 CSS 动画的元素：把位移挪到外层 g，动画只绕自身旋转
if(a.transform&&ANIM[id]){delete p.transform;return React.createElement('g',{key,transform:a.transform},el);}
return el;
};
// 状态机：照搬看板页面的 STAGE_CONFIG + setStep + setStage
const STAGE={
IDLE:{tone:'slate',badge:'STANDBY · 待命',pipes:[],log:'[系统] ⚡ 流水线待命中，无活跃执行任务',details:'可通过 flow_dispatch 派发新任务，或点击下方按钮演练全流程'},
SPEC:{tone:'blue',badge:'① 方案/测试 · 待审批',pipes:['pipe-1-2'],node:'node-spec',color:'#3b82f6',log:'[方案/测试] 📐 蓝图契约完成，验收测试就绪，待人工审批',details:'用例进入门禁-1盲测，必须证明能捕获未实现方可准入'},
WORK:{tone:'amber',badge:'⑤ 编码施工 · 目标全绿',pipes:['pipe-5-6'],node:'node-work',color:'#d97706',log:'[编码施工] 🔨 工兵在独立沙箱编写代码，白名单监控中',details:'超范围修改即时拦截，测试文件禁止改动'},
TEST:{tone:'purple',badge:'⑥ 自动化测试 · 跑道执行',pipes:['pipe-6-8'],node:'node-test',color:'#3b82f6',log:'[自动化测试] ⚙️ 防篡改审计 + ruff 静态检查 + pytest 验收（可选全量回归）',details:'确定性执行：未过退回工兵修复；全通放行送审'},
CRITIC:{tone:'cyan',badge:'③ 前置审查 · 契约双审',pipes:['pipe-3-4'],node:'node-critic',color:'#0891b2',log:'[前置审查] 🧐 审查随契约起草内嵌执行，核对设计文档与验收用例一致性',details:'结论写入 spec.pre_critic_report；独立状态位预留，不单独流转'},
MR:{tone:'emerald',badge:'⑧ 成果交付 · 任务竣工',pipes:['pipe-6-8'],node:'node-mr',color:'#10b981',log:'[成果交付] 🚀 成果安全存入代码库，自动发起 PR/MR',details:'全套测试存证完备，符合交付准入规范'},
ESCALATE:{tone:'rose',badge:'🚨 熔断跳闸 · 自愈环停机',pipes:['pipe-branch-escalate'],node:'node-escalate',color:'#ef4444',log:'[熔断跳闸] 🚨 自愈重试超限自动熔断停机，保护工作现场',details:'主干零污染，等待人工排障复位'},
ABORT:{tone:'rose',badge:'⛔ 已中止 · ABORTED',pipes:[],log:'[异常中止] ⛔ 任务已被人工中止或系统终止，现场已保护',details:'主干零污染；可 flow_dispatch 派发新任务，或在日志区回看中止前现场'}};
const STEPS={
1:{stage:'SPEC',nodes:{'node-spec':'#2563eb'},avatar:['svg-architect','#3b82f6'],pipes:['pipe-1-2'],log:'[1. 架构设计] 📐 架构师 AI 基于 AST 影响面推导产出设计文档与任务拆分',details:'推导影响面爆炸半径与受影响调用点，拆解原子任务清单'},
2:{stage:'SPEC',nodes:{'node-step-2':'#16a34a'},avatar:['svg-tester','#10b981'],pipes:['pipe-2-3'],log:'[2. 红色测试] 🧪 测试 AI 依据设计规范编写用例，真机预检红灯有效并通过变异击杀率验真',details:'基线红灯确认防空跑假需求，变异击杀率 ≥85% 方可锁定'},
3:{stage:'CRITIC',nodes:{'node-critic':'#0891b2'},avatar:['svg-critic','#06b6d4'],pipes:['pipe-3-4'],log:'[3. 前置审查] 🧐 独立审查员核对设计与用例一致性及架构规范（随契约起草内嵌执行）',details:'审查结论写入 spec.pre_critic_report；AST 语义与 eval/exec 检查在交付门禁执行'},
4:{stage:'SPEC',nodes:{'badge-hitl-spec':'#d97706'},avatar:null,pipes:['pipe-4-5'],log:'[4. 关键 HITL] ✍️ 人工确认任务拆分与测试用例，签字锁定契约规范',details:'锁定时强制执行变异击杀率 ≥85% 门禁，契约一旦锁定不可篡改'},
5:{stage:'WORK',nodes:{'node-work':'#d97706'},avatar:['svg-builder','#f59e0b'],pipes:['pipe-5-6'],log:'[5. 沙箱编码] 🔨 开发工兵进入隔离沙箱 Worktree 开始编码施工',details:'白名单受限修改，严格执行 TDD 契约'},
6:{stage:'TEST',nodes:{'node-test':'#7c3aed'},avatar:null,pipes:['pipe-6-8'],log:'[6. 自动测试] ⚙️ 机械跑道执行防篡改审计、ruff 静态检查与 pytest 验收跑测',details:'无副作用自动化测试，验证业务功能完整通过'},
7:{stage:'WORK',nodes:{'node-work':'#d97706','node-escalate':'#f97316'},avatar:['svg-builder','#f59e0b'],pipes:['pipe-loop-tdd'],log:'[7. 自愈打回] 🔄 测试未通过，提取错误指纹打回开发工兵定向修复',details:'AST 影响点与异常回溯自愈，重试计数累计 (≤3次)'},
8:{stage:'MR',nodes:{'node-mr':'#059669'},avatar:null,pipes:['pipe-6-8'],log:'[8. 成果交付] 🚀 AST 语义硬门禁一票否决校验通过，成果安全合入代码库并交付',details:'检查无破坏性变更与规则违背，准予合并交付主干'},
9:{stage:'ESCALATE',nodes:{'node-escalate':'#dc2626'},avatar:null,pipes:['pipe-branch-escalate'],log:'[熔断跳闸] 🚨 自愈重试超限触发物理熔断，启动保护机制',details:'避免无限循环与主干破坏，等待人工排障复位'}};
const STEP_TITLE=['待命 · STANDBY','步骤 1/8 · 架构设计 AI (方案与任务拆分)','步骤 2/8 · 验收测试 AI (红灯预检·变异验真)','步骤 3/8 · 前置审查 (契约内嵌·一致性双审)','步骤 4/8 · 关键 HITL (人工签字锁定)','步骤 5/8 · 开发工兵 AI (沙箱编码施工)','步骤 6/8 · 自动化测试 (防篡改·lint·验收跑测)','步骤 7/8 · 定向自愈重试 (错误指纹打回)','步骤 8/8 · AST 硬门禁 + 成果交付代码库','🚨 熔断跳闸 (自愈环保护停机)'];
function derive(step,opt){
const s=(opt&&opt.escalated&&step===7)?STEPS[9]:STEPS[step];
let stage=STAGE[s?s.stage:'IDLE'];
if(step===0&&opt&&opt.abort)stage=STAGE.ABORT;
const nodes={},visible={},anim={};
if(stage.node)nodes[stage.node]=stage.color;
stage.pipes.forEach(id=>{visible[id]=1;});
let avatar=null;
if(s){Object.assign(nodes,s.nodes);s.pipes.forEach(id=>{visible[id]=1;});avatar=s.avatar;}
if(step===6&&opt.highlightCritic){nodes['node-critic']='#0891b2';avatar=['svg-critic','#06b6d4'];}
if(stage===STAGE.ESCALATE)visible['svg-siren-rays']=1;
if(stage===STAGE.WORK)anim['svg-hammer']='hammer';
if(stage===STAGE.SPEC)anim['svg-tube']='tube';
if(stage===STAGE.CRITIC)anim['svg-glass']='glass';
if(stage===STAGE.MR)anim['svg-truck']='truck';
if(stage===STAGE.TEST)anim['svg-runner-gears']='spin';
return {nodes,visible,anim,avatar:avatar?avatar[0]:null,avatarColor:avatar?avatar[1]:'',tone:stage.tone,badge:stage.badge,log:s?s.log:stage.log,details:s?s.details:stage.details,stepTitle:(step===0&&opt&&opt.abort)?'⛔ 任务已中止':(STEP_TITLE[step]||''),onStep:()=>{}};
}
const PILLS=[[1,'设计AI 📐','方案/拆分','1. 架构设计AI产出设计文档与任务拆分','#2563eb'],[2,'测试AI 🧪','红灯真测','2. 测试AI根据设计出红色测试用例(AST影响点)','#059669'],[3,'Critic 🧐','双审一致','3. 前置审查核对设计与用例一致性（随契约起草内嵌执行）','#0891b2'],[4,'HITL ✍️','签字锁定','4. 关键HITL确认任务和测试cases并锁定','#d97706'],[5,'开发AI 🔨','沙箱施工','5. 开发AI在受限沙箱开始编码','#d97706'],[6,'自动测试 ⚙️','真机跑测','6. 对开发结果自动化测试跑道真机验证','#7c3aed'],[7,'自愈打回 🔄','指纹修复','7. 测试不通过打回开发AI定向自愈重试','#ea580c'],[8,'仓库交付 🚀','硬门禁入库','8. AST硬门禁防线 + 交付主干仓库','#059669']];
const GATES=[['👤 关卡-1','人工签字确认 (方案与测试审核)',[4],'amber','人工签字'],['🔒 门禁-1','验证测试真实有效 (必须报错防假需求)',[2],'blue','红灯验真'],['🔒 门禁-M','变异击杀率 ≥85% (锁定时强制，度量步骤2用例有效性)',[2,4],'cyan','变异验真'],['🔒 门禁-2','划分独立工位 (改动范围锁定防污染)',[5,7],'orange','沙箱隔离'],['🔒 门禁-3','机器自动测试 (全套用例真机跑通)',[6],'purple','机械跑道'],['👤 关卡-2','异常跳闸保护 (人工介入排障复位)',[9],'rose','异常复位']];
const SIM=[['1.设计AI 📐','blue'],['2.测试AI 🧪','emerald'],['3.Critic 🧐','cyan'],['4.HITL ✍️','amber'],['5.开发AI 🔨','amber'],['6.自动测试 ⚙️','purple'],['7.自愈打回 🔄','orange'],['8.仓库交付 🚀','emerald'],['熔断 🚨','rose']];
const STEP_HERO={
0:{stage:'系统待命 · STANDBY',actor:'系统调度器',action:'流水线处于空闲待命状态，无活跃执行任务',waiting:'等待派发新任务 (flow_init / flow_dispatch)',station:'工位 0 · 待命区',tone:'slate',dot:'#64748b',badge:'⚡ 待命待发'},
1:{stage:'步骤 1/8 · 架构蓝图与任务拆分',actor:'架构师 AI (Architect Agent)',action:'架构师 AI 正在分析代码依赖与 AST 影响面，起草设计文档与原子任务清单',waiting:'等待架构设计文档生成',station:'工位 1 · 架构设计工位',tone:'blue',dot:'#2563eb',badge:'📐 架构师方案起草中'},
2:{stage:'步骤 2/8 · 验收测试编写与变异验真',actor:'测试工程师 AI (QA Tester Agent)',action:'测试 AI 正在编写验收用例，执行真实红灯预检与 AST 变异测试击杀分析 (要求 ≥85%)',waiting:'等待测试编写与变异门禁判定',station:'工位 2 · 验收测试实验室',tone:'emerald',dot:'#059669',badge:'🧪 验收测试编写与验真中'},
3:{stage:'步骤 3/8 · 前置规范双审',actor:'独立审查专家 (Critic Agent)',action:'第三视角审查员正在双审设计与用例一致性，校验架构规范与公共签名保护',waiting:'等待前置双审放行',station:'工位 3 · 前置双审台',tone:'cyan',dot:'#0891b2',badge:'🧐 前置规范审查中'},
4:{stage:'步骤 4/8 · 关键 HITL 契约锁定',actor:'人工审核员 / 调度器 (HITL)',action:'任务契约与测试用例已锁定签名，环境准备就绪，等待派发沙箱编码施工',waiting:'等待调用 flow_dispatch 启动工兵沙箱开发',station:'工位 4 · 签字锁定期',tone:'amber',dot:'#d97706',badge:'✍️ 契约已锁定 · 等待派发'},
5:{stage:'步骤 5/8 · 开发工兵沙箱编码施工',actor:'开发工兵 Agent (Worker Agent)',action:'开发工兵 Agent 正在独立沙箱 Worktree 中受限编码施工，目标全量跑通验收测试',waiting:'等待工兵编码完成提交',station:'工位 5 · 沙箱编码施工区',tone:'amber',dot:'#d97706',badge:'🔨 开发工兵沙箱编码施工中'},
6:{stage:'步骤 6/8 · 自动化测试全量真机跑测',actor:'自动化测试跑道引擎 (Test Runner)',action:'机械跑道正在执行测试防篡改审计、Ruff 规范静态扫描与全量 Pytest 真机跑测',waiting:'等待自动化测试全绿通过',station:'工位 6 · 自动化测试跑道',tone:'purple',dot:'#7c3aed',badge:'⚙️ 自动化测试真机跑测中'},
7:{stage:'步骤 7/8 · 错误指纹定向自愈回流',actor:'自愈修复工兵 (Self-Healing Worker)',action:'测试未通过，系统已提取错误指纹回路送回工兵工位进行定向修复 (≤3轮)',waiting:'等待工兵修复并重新送测',station:'工位 7 · 自愈回流环',tone:'orange',dot:'#ea580c',badge:'🔄 错误指纹定向自愈修复中'},
8:{stage:'步骤 8/8 · AST 硬门禁 + 成果交付代码库',actor:'代码仓库评审人 (Code Reviewer)',action:'全套测试验证通过，代码已安全存入仓库，已生成 PR/MR 并附带全套存证，等待人工合并评审',waiting:'等待人工在 GitLab/GitHub 上评审并合并 PR/MR',station:'工位 8 · 成果交付代码库',tone:'emerald',dot:'#059669',badge:'🎉 成果交付就绪 · 等待合并评审'},
9:{stage:'自愈环熔断 · 异常跳闸保护',actor:'物理熔断保护器 (Circuit Breaker)',action:'自愈重试超限或出现严重异常，自愈环触发熔断强制停机，保护工作现场未受污染',waiting:'等待人工排障复位 (调用 flow_resume 或 flow_abort)',station:'工位 7 · 自愈回流环',tone:'rose',dot:'#dc2626',badge:'🚨 异常跳闸 · 等待人工排障'}
};
const ACTIVE_PILL_SUB={1:'📐 起草方案',2:'🧪 编写/变异',3:'🧐 前置双审',4:'✍️ 契约待发',5:'🔨 正在编码',6:'⚙️ 正在跑测',7:'🔄 自愈修复',8:'🚀 交付就绪'};
const call=(method,args)=>host.call('pixel_forge',method,args);
const reason=e=>String((e&&e.message)||e);
function Screen(props){
const t=ctx.locale.useT(ns);
const [data,setData]=React.useState(null),[runs,setRuns]=React.useState([]),[runId,setRunId]=React.useState('');
const [online,setOnline]=React.useState(false),[fault,setFault]=React.useState(null),[notice,setNotice]=React.useState(''),[starting,setStarting]=React.useState(false);
const [sim,setSim]=React.useState(null),[auto,setAuto]=React.useState(false),[tick,setTick]=React.useState(0);
// 轮询：status 每秒一次，runs 每 5 轮一次；连不上时放慢到 3 秒。宿主方法不抛异常，失败以 {ok:false,reason,url} 返回
React.useEffect(()=>{
let alive=true,timer=null,n=0;
function fail(f){if(!alive)return;setOnline(false);setFault(f);timer=setTimeout(loop,3000);}
let ensured=false;
async function loop(){
try{
const status=await call('status',[runId]);
if(!alive)return;
if(!status||status.ok!==true){const unreach=!status||status.reason==='unreachable';if(unreach&&!ensured){ensured=true;setStarting(true);call('ensure').then(r=>{if(!alive)return;setStarting(Boolean(r&&!r.up));timer=setTimeout(loop,2500);},e=>{if(!alive)return;setStarting(false);fail({kind:'internal',text:reason(e)});});return;}fail({kind:status&&status.reason,status:status&&status.status,url:status&&status.url});return;}
setData(status.data);setOnline(true);setFault(null);setStarting(false);ensured=false;
if(n%5===0){const list=await call('runs');if(!alive)return;setRuns((list&&list.ok&&list.data&&list.data.runs)||[]);}
n++;timer=setTimeout(loop,1000);
}catch(e){fail({kind:'internal',text:reason(e)});}
}
loop();
return ()=>{alive=false;if(timer)clearTimeout(timer);};
},[runId,tick]);
React.useEffect(()=>{
if(!auto)return;
const id=setInterval(()=>setSim(s=>({step:((s&&s.step)||0)%8+1})),2200);
return ()=>clearInterval(id);
},[auto]);
const play=step=>{setAuto(false);setSim(step?{step}:null);setNotice('');};
const toggleAuto=()=>{if(auto){setAuto(false);setSim(null);}else{setSim({step:1});setAuto(true);}};
const openFull=()=>call('open').then(r=>setNotice(t('opened')+((r&&r.url)||'')),e=>setNotice(reason(e)));
const start=()=>call('ensure').then(r=>{setStarting(Boolean(r&&r.starting));if(r&&r.up)setTick(x=>x+1);},e=>setFault({kind:'internal',text:reason(e)}));
const faultText=!fault?'':fault.kind==='internal'?fault.text:(fault.url||'')+' · '+(fault.kind==='http'?'HTTP '+(fault.status||'?'):t('noResponse'));
const liveStep=data&&typeof data.step==='number'?data.step:0,step=sim?sim.step:liveStep;
const st=derive(step,{highlightCritic:Boolean(data&&data.highlight_critic),abort:Boolean(data&&data.status==='ABORTED'),escalated:Boolean(data&&data.status==='ESCALATED')});
st.onStep=play;
const d=data||{};
const primary=sim?st.log:((data&&(d.last_log||d.message))||st.log);
const rows=[];
const add=(k,v,title,tone)=>rows.push({k,v,title,tone});
if(d.run_id)add(t('run'),d.run_id);
if(d.allowed_paths&&d.allowed_paths.length)add(t('allowed'),d.allowed_paths.join(', '));
if(d.locked_test)add(t('lockedTest'),d.locked_test);
if(d.worktree_path)add(t('worktree'),d.worktree_path);
if(d.run_id)add(t('target'),d.target_branch||'main');
if(d.design_doc)add(t('design'),'已锁定 · 任务拆分就绪',d.design_doc);
if(d.red_evidence)add(t('red'),(d.red_evidence.verdict||'PASSED_RED')+' (基线红灯验证有效)',JSON.stringify(d.red_evidence));
if(d.mutation_report){const m=d.mutation_report;add(t('mutation'),(Math.round((m.kill_ratio||0)*1000)/10)+'% ('+(m.killed_count||0)+'/'+(m.total_mutants||0)+' 击杀'+(m.is_acceptable?' · 达标':' · 未达标')+')',JSON.stringify(m),m.is_acceptable?'emerald':'rose');}
if(d.pre_critic_report)add(t('preCritic'),d.pre_critic_report.approved?'规范双审一致 (准予放行)':'存在拦截项 (打回修改)','',d.pre_critic_report.approved?'emerald':'rose');
if(d.impact_analysis){const callers=d.impact_analysis.impacted_callers||[];add(t('impact'),callers.length+' 处受影响调用点',callers.join(', '));}
if(d.updated_at)add(t('updated'),String(d.updated_at).slice(0,19).replace('T',' '));
const rev=d.review,issues=(rev&&(rev.blocking_issues||[]))||[];
const heroToneMap={WORK:'amber',TEST:'purple',MR:'emerald',ESCALATE:'rose',CRITIC:'cyan',SPEC:'blue',IDLE:'slate'};
const heroDotMap={amber:'#d97706',purple:'#7c3aed',emerald:'#059669',rose:'#dc2626',cyan:'#0891b2',blue:'#2563eb',slate:'#64748b'};
const dNode=d.node_info;
const hero=sim?STEP_HERO[sim.step]:dNode?{
stage:dNode.stage_name||st.stepTitle,
actor:dNode.actor_role||d.active_actor,
action:dNode.current_action||d.current_action,
waiting:dNode.waiting_for||d.waiting_for,
station:dNode.station_name,
tone:heroToneMap[dNode.node_key]||'blue',
dot:heroDotMap[heroToneMap[dNode.node_key]]||'#2563eb',
badge:dNode.badge_text||st.badge
}:STEP_HERO[liveStep]||STEP_HERO[0];
return h('div',{className:'pfs'+(props.page&&props.page.standalone?' pfs-full':'')},
h('header',{className:'pfs-card pfs-row pfs-bar'},
h('div',{className:'pfs-brand'},h('span',{className:'pfs-logo','aria-hidden':'true'},'⚡'),
h('div',null,h('div',{className:'pfs-title'},'ADK '+t('title'),h('span',{className:'pfs-tag'},'Web UI')),h('div',{className:'pfs-sub'},t('sub')))),
h('div',{className:'pfs-row'},h('span',{className:'pfs-led'+(online?' pfs-led-on':'')}),h('span',{className:'pfs-badge pfs-t-'+st.tone,role:'status'},st.badge)),
h('div',{className:'pfs-row'},
d.repo_name?h('span',{className:'pfs-chip',title:t('run')},'🐙 ',d.repo_name):null,
d.branch?h('span',{className:'pfs-chip',title:d.branch},'🌿 ',d.branch):null,
d.merged_after_abort?h('span',{className:'pfs-chip pfs-chip-ma',title:'该 run 已中止，但其分支已合并进主分支'},'🔗 已合并 · 流程中止'):null,
h('select',{'aria-label':t('currentRun'),className:'pfs-select',value:runId,onChange:e=>setRunId(e.target.value)},
h('option',{key:'cur',value:''},t('currentRun')),
runs.map(r=>h('option',{key:r.run_id,value:r.run_id},r.run_id+' ('+r.status+(r.merged_after_abort?' · 已合并':'')+')'))),
h('button',{type:'button',className:'pfs-btn',title:t('refresh'),'aria-label':t('refresh'),onClick:()=>setTick(x=>x+1)},'🔄'),
h('button',{type:'button',className:'pfs-btn pfs-btn-pri',disabled:!online,onClick:openFull},t('openFull')),
sim?h('button',{type:'button',className:'pfs-btn pfs-btn-warn',onClick:()=>play(0)},t('backLive')):null,
h('button',{type:'button',className:'pfs-btn '+(auto?'pfs-btn-rose':'pfs-btn-indigo'),onClick:toggleAuto},auto?t('stopAuto'):t('autoPlay')))),
d.outside_activity?h('div',{className:'pfs-oab',role:'note'},
h('b',null,'⚠ 流程外工作'),
h('span',null,(d.outside_activity.branch||'')+' @ '+(d.outside_activity.short_sha||'')),
h('span',{className:'pfs-sub'},d.outside_activity.subject||''),
h('span',{className:'pfs-sub'},d.outside_activity.hint||'')):null,
h('section',{className:'pfs-hero',role:'region','aria-label':t('currentNode')},
h('div',{className:'pfs-hero-main'},
h('div',{className:'pfs-hero-beacon'},
h('span',{className:'pfs-hero-ping pfs-t-'+hero.tone,style:{background:hero.dot}}),
h('span',{className:'pfs-hero-dot',style:{background:hero.dot}})),
h('div',{className:'pfs-hero-content'},
h('div',{className:'pfs-hero-top'},
h('span',{className:'pfs-hero-lbl'},t('currentNode')),
h('span',{className:'pfs-hero-stage pfs-t-'+hero.tone},hero.badge||hero.stage),
hero.actor?h('span',{className:'pfs-hero-actor'},t('activeActor')+': '+hero.actor):null),
h('div',{className:'pfs-hero-action'},hero.action))),
h('div',{className:'pfs-hero-side'},
hero.waiting?h('div',{className:'pfs-hero-box'},
h('span',{className:'pfs-hero-box-lbl'},t('waitingFor')+':'),
h('b',{style:{color:'#d97706'}},hero.waiting)):null,
hero.station?h('div',{className:'pfs-hero-box'},
h('span',{className:'pfs-hero-box-lbl'},t('stationPos')+':'),
h('b',{style:{color:'#4f46e5'}},hero.station)):null)),
!online?h('div',{role:'alert',className:'pfs-banner pfs-banner-off'},h('span',null,fault?t('offline'):t('loading')),faultText?h('code',{className:'pfs-err'},faultText):null,h('button',{type:'button',className:'pfs-btn pfs-btn-pri',onClick:start},t('startServer'))):null,
starting?h('div',{role:'status',className:'pfs-banner'},t('starting')):null,
sim?h('div',{role:'status',className:'pfs-banner pfs-banner-sim'},t('simOn')):null,
notice?h('div',{role:'status',className:'pfs-banner'},notice):null,
h('div',{className:'pfs-card pfs-row'},h('span',{className:'pfs-gates-title'},'🛡️ ',t('gates')),
GATES.map(g=>h('span',{key:g[0],className:'pfs-gate pfs-t-'+g[3]+(g[2].indexOf(step)>=0?' pfs-gate-on':''),title:g[1]},h('span',null,g[0]),h('b',null,g[4])))),
h('section',{className:'pfs-card'},
h('div',{className:'pfs-sec-head'},h('span',null,'🔄 ',t('lifecycle')),h('span',{className:'pfs-badge pfs-t-blue',role:'status'},st.stepTitle)),
h('div',{className:'pfs-steps'},PILLS.map(m=>{
const isCur=step===m[0];
const isDone=step>m[0];
const subLabel=isCur?(ACTIVE_PILL_SUB[m[0]]||m[2]):isDone?'已完成 ✓':m[2];
return h('button',{key:m[0],type:'button',title:m[3],'aria-label':m[1],className:'pfs-pill '+(isCur?'pfs-pill-on':isDone?'pfs-pill-done':'pfs-pill-todo'),onClick:()=>play(m[0])},
h('span',null,h('span',{className:'pfs-num',style:{background:m[4]+'22',color:m[4]}},String(m[0])),m[1]),
h('span',{className:'pfs-pill-sub'+(isCur?' pfs-pill-active-sub':'')},subLabel));
}))),
h('section',{className:'pfs-card pfs-canvas'},render(ROOT,st)),
h('div',{className:'pfs-cols'},
h('section',{className:'pfs-card pfs-panel'},h('h3',{className:'pfs-h3'},'📋 ',t('contract')),
rows.length?rows.map((r,i)=>h('div',{key:i,className:'pfs-kv'},h('span',{className:'pfs-k'},r.k),h('span',{className:'pfs-v'+(r.tone?' pfs-t-'+r.tone:''),title:r.title||String(r.v)},String(r.v)))):h('p',{className:'pfs-muted'},t('noRun')),
rev?h('div',{className:'pfs-review'},
h('div',{className:'pfs-row pfs-spread'},h('b',null,'🧐 ',t('verdict')),h('span',{className:'pfs-badge '+(rev.approved?'pfs-t-emerald':'pfs-t-rose')},rev.approved?t('passed'):t('blocked'))),
h('div',null,rev.summary||(rev.approved?'审查确认白名单合规且用例通过，准予交付':'审查未通过，存在阻断项')),
issues.length?h('div',{className:'pfs-issues'},issues.map((x,i)=>h('div',{key:i},'• '+x))):null):null,
h('div',{className:'pfs-lock'},t('lockNote'))),
h('section',{className:'pfs-card pfs-panel'},
h('div',{className:'pfs-row pfs-spread'},h('span',{className:'pfs-row'},h('i',{className:'pfs-dot',style:{background:'#fb7185'}}),h('i',{className:'pfs-dot',style:{background:'#fbbf24'}}),h('i',{className:'pfs-dot',style:{background:'#34d399'}}),h('span',{className:'pfs-mono pfs-file'},'adk-dev-flow.log')),
h('span',{className:'pfs-badge '+(online?'pfs-t-slate':'pfs-t-rose')},t('attempt')+': '+(d.attempt||0)+' / 3')),
h('div',{className:'pfs-term'},h('div',{className:'pfs-term-p'},h('span',{className:'pfs-prompt'},'❯'),h('span',null,primary)),h('div',{className:'pfs-term-s'},st.details)),
h('div',{className:'pfs-row pfs-spread pfs-simhead'},h('b',null,'🎮 ',t('simDock')),h('span',{className:'pfs-muted'},t('simDockHint'))),
h('div',{className:'pfs-simg'},SIM.map((b,i)=>h('button',{key:i,type:'button',className:'pfs-simbtn pfs-t-'+b[1]+(step===i+1?' pfs-simbtn-on':''),onClick:()=>play(i+1)},b[0]))))));
}
// 整页入口：侧边栏最上方原生入口 + 路由 #/app/pixel-forge，?standalone 为无外壳大屏
ctx.slots.register({name:'app.page',id:'pixel-forge',order:0,label:()=>ctx.locale.t(ns,'title'),icon:'⚡'},Screen);
}};
})(React,host,styles,console);
export default {...plugin,slotContract:plugin.slotContract??'legacy'};
