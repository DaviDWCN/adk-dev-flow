# 像素工坊大屏

启用后侧边栏最上方出现「像素工坊大屏」入口，点开即整页看板；页面每秒同步看板服务。每个会话的大屏只看自己仓库：宿主侧从会话根向上定位最近的 `.adk/workflow.db` 作为仓库根，按仓库路径派生确定性端口（21000-28999）拉起独立看板服务，多会话 / 多仓库互不抢占；设置 `ADK_DASHBOARD_PORT` 则退回全局单端口（默认 8080）。点工位卡片或底部按钮演练各步骤，「在浏览器打开完整看板」跳转原始看板网页。

Exported source: pixel-forge-screen revision 7, hash 6304d3d2701acab87d34daed5af69fcf2cf50f855fee7e25fb43f9efaa6bc56c.
No user data, source grants or installation state is included.
