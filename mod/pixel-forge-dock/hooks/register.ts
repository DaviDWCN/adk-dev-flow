/**
 * pixel-forge-dock — PoC Mod
 *
 * Verifies in CodeBuddy Web UI:
 *   1. `$.ui.open` creates a persistent dock pane (aside.mod-dock)
 *   2. `ui.render {component:'Pane'}` draws a `Svg` element into the dock
 *   3. SVG animations survive: SMIL <animateTransform> and CSS @keyframes
 *   4. Button press roundtrip: ui.press -> $.store -> $.ui.invalidate -> redraw
 */
import type { On } from 'codebuddy-code';

const PANE_ID = 'pixel-forge';
const TITLE = '像素工坊看板 (PoC)';
const INC_KEY = 'pf-inc';
const RESET_KEY = 'pf-reset';

type AnyEngine = any;

function buildSvg(count: number, now: string, surface: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 180">
  <style>
    @keyframes pf-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.12; } }
    @keyframes pf-spin  { to { transform: rotate(360deg); } }
    .pf-pulse { animation: pf-pulse 1.5s ease-in-out infinite; }
    .pf-spin  { transform-box: fill-box; transform-origin: center; animation: pf-spin 3s linear infinite; }
  </style>
  <rect x="0" y="0" width="480" height="180" rx="8" fill="#0f172a"/>
  <text x="14" y="24" fill="#7dd3fc" font-size="13">像素工坊 PoC · ${now}</text>
  <text x="14" y="44" fill="#e2e8f0" font-size="12">count = ${count} · surface = ${surface}</text>
  <rect x="14" y="58" width="48" height="48" rx="6" fill="#38bdf8">
    <animateTransform attributeName="transform" type="rotate" from="0 38 82" to="360 38 82" dur="3s" repeatCount="indefinite"/>
  </rect>
  <circle cx="200" cy="82" r="26" fill="#f59e0b" class="pf-pulse"/>
  <rect x="300" y="58" width="48" height="48" rx="6" fill="#10b981" class="pf-spin"/>
  <rect x="380" y="58" width="48" height="48" rx="6" fill="#475569"/>
  <text x="14" y="126" fill="#64748b" font-size="11">A SMIL rotate · B CSS pulse · C CSS spin · D static</text>
  <text x="14" y="146" fill="#64748b" font-size="11">若 A/B/C 在动而 D 静止，则动画链路验证通过</text>
</svg>`;
}

async function drawPane($: AnyEngine, e: AnyEngine, next: AnyEngine): Promise<AnyEngine> {
  if (e.requestId !== PANE_ID) return next(e);
  const beneath = await next(e);
  const el = $.ui.resolve(e);
  const count = (await $.store.get('count')) ?? 0;
  const now = new Date().toISOString().slice(11, 19);

  if (e.surface === 'terminal') {
    const tree = el.Box({
      flexDirection: 'column',
      children: [
        el.Text({ children: `${TITLE} · count=${count} · ${now}` }),
        el.Text({ children: 'SVG 看板仅在 Web UI dock 渲染（TUI 文本兜底）' }),
      ],
    });
    return { ...beneath, tree };
  }

  const tree = el.Box({
    flexDirection: 'column',
    children: [
      el.Svg({
        source: buildSvg(count, now, String(e.surface)),
        alt: '像素工坊 PoC：三种 SVG 动画变体与计数器',
        width: 480,
        sandbox: true,
      }),
      el.Box({
        flexDirection: 'row',
        children: [
          el.Button({ key: INC_KEY, label: '+1', onPress: async () => { /* handled in ui.press */ } }),
          el.Button({ key: RESET_KEY, label: '归零', onPress: async () => { /* handled in ui.press */ } }),
        ],
      }),
      el.Text({ children: `count = ${count}` }),
      el.Text({ children: `dock pane · ${now}` }),
    ],
  });
  return { ...beneath, tree };
}

export function register(on: On): void {
  on('session.start', async ($: AnyEngine, e: AnyEngine, next: AnyEngine) => {
    const r = await next(e);
    try {
      const res = await $.ui.open({ id: PANE_ID, title: TITLE });
      if (res && res.isPlaced === false) {
        $.ui.log(`[pixel-forge] pane opened but not placed: ${res.reason ?? '?'}`);
      }
    } catch (err) {
      $.ui.log(`[pixel-forge] ui.open failed: ${String(err)}`);
    }
    return r;
  });

  on('ui.render', { component: 'Pane' }, drawPane);

  on('ui.press', async ($: AnyEngine, e: AnyEngine, next: AnyEngine) => {
    $.ui.log(`[pixel-forge] press: element=${e.element} surface=${e.surface}`);
    if (e.element === INC_KEY || e.element === RESET_KEY) {
      try {
        const c = (await $.store.get('count')) ?? 0;
        const n = e.element === INC_KEY ? Number(c) + 1 : 0;
        await $.store.set('count', n);
        $.ui.log(`[pixel-forge] store.set count=${n}`);
        await $.ui.invalidate('ui.render');
      } catch (err) {
        $.ui.log(`[pixel-forge] press failed: ${String(err)}`);
        return { element: e.element, deny: String(err) };
      }
      return { element: e.element };
    }
    return next(e);
  });
}
