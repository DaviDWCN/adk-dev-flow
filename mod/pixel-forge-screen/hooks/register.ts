/**
 * pixel-forge-screen — 像素工坊大屏（宿主侧）
 *
 * 页面里的模组代码不能发网络请求；取数、拉起服务、调系统浏览器都经这里的
 * engine.create 自定义名词 pixel_forge，由页面 host.call('pixel_forge', 方法, [参数]) 调用：
 *   status(runId?)  读看板服务 /api/status
 *   runs()          读 /api/runs
 *   ensure()        探活；未启动则后台拉起看板服务
 *   open()          ensure 后用系统默认浏览器打开完整看板
 *
 * 每个会话自己的大屏：从会话根向上定位最近的 .adk/workflow.db 作为仓库根，
 * 按仓库路径派生确定性端口（21000-28999），多会话 / 多仓库互不抢占；
 * 设置环境变量 ADK_DASHBOARD_PORT 则退回全局单端口行为。
 * 读接口不抛异常：连不上或响应异常时返回 {ok:false,reason,url}，由页面如实展示原因。
 */
export function register(on) {
  on('engine.create', async (_$, e, next) => {
    const beneath = await next(e);
    let cached = null;
    const cfg = async () => {
      if (cached) return cached;
      const root = String((await beneath.session.root()) ?? '.');
      // 会话根可能是仓库子目录：向上找最近的 .adk/workflow.db 作为仓库根
      let repo = root;
      try {
        const hits = await beneath.fs.ancestors({ names: ['.adk/workflow.db'], below: root });
        if (hits && hits.length) repo = String(hits[0].dir ?? root);
      } catch {}
      const envPort = await beneath.env.get('ADK_DASHBOARD_PORT');
      let port = String(envPort || '');
      if (!port) {
        // 按仓库路径派生确定性端口（FNV-1a）
        let h = 2166136261;
        for (let i = 0; i < repo.length; i++) {
          h ^= repo.charCodeAt(i);
          h = Math.imul(h, 16777619);
        }
        port = String(21000 + ((h >>> 0) % 8000));
      }
      cached = { port, repo, url: `http://127.0.0.1:${port}/` };
      return cached;
    };
    const read = async path => {
      const { url } = await cfg();
      try {
        const res = await beneath.http.fetch(url.replace(/\/$/, '') + path);
        const text = res && typeof res.text === 'string' ? res.text : '';
        const ok = Boolean(res && (res.ok === true || res.status === 200));
        if (!ok) return { ok: false, reason: 'http', status: res ? res.status : null, url };
        return { ok: true, data: JSON.parse(text), url };
      } catch (error) {
        // 服务未启动（拒绝连接）时宿主只抛 Internal error，这里转成结构化结果交给页面展示
        return { ok: false, reason: 'unreachable', status: null, url, error: String(error) };
      }
    };
    const ensure = async () => {
      const { port, repo, url } = await cfg();
      if ((await read('/api/status')).ok) return { up: true, starting: false, url };
      await beneath.process.run([
        'python',
        '-m',
        'adk_dev_flow.dashboard',
        '--detach',
        '--host',
        '127.0.0.1',
        '--port',
        port,
        '--repo',
        repo,
      ]);
      return { up: false, starting: true, url };
    };
    const open = async () => {
      const result = await ensure();
      const osEnv = await beneath.env.get('OS');
      const plat = String(globalThis.process?.platform ?? '');
      const isWin = plat === 'win32' || String(osEnv ?? '').startsWith('Windows');
      const argv = isWin
        ? ['cmd', '/c', 'start', '', result.url]
        : plat === 'darwin'
          ? ['open', result.url]
          : ['xdg-open', result.url];
      await beneath.process.run(argv);
      return { opened: true, url: result.url };
    };
    return {
      ...beneath,
      pixel_forge: {
        status: runId =>
          read(runId ? `/api/status?run_id=${encodeURIComponent(String(runId))}` : '/api/status'),
        runs: () => read('/api/runs'),
        ensure,
        open,
      },
    };
  });
}
