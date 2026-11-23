import { describe, expect, mock, test, tier } from 'codebuddy-code/testing';

tier('user');

/**
 * The mod under test is auto-loaded by `plugin test`; `on` hooks sit at the
 * world layer (beneath the mod) and stub every `$` call the mod makes.
 * Un-stubbed `$` calls throw and name the event.
 */
const SURFACE = 'desktop'; // has Svg (terminal does not; cbc-webui is not a test surface)

function stubUiCalls(on: any, opened?: any[]): void {
  on('ui.open', ($: any, e: any) => {
    if (opened) opened.push(e);
    return { value: { isPlaced: true } };
  });
  on('ui.log', () => ({ value: {} }));
  on('ui.toast', () => ({ value: {} }));
  // beneath the mod nothing paints Panes; give the chain a beneath answer
  on('ui.render', () => ({}));
}

describe('pixel-forge-dock', () => {
  test('session.start opens the dock pane', async ($: any, on: any) => {
    const opened: any[] = [];
    stubUiCalls(on, opened);
    mock.store(on, { count: 0 });
    on('session.start', () => ({ cwd: '.' }));

    await $.session.start({ cwd: '.' });

    expect(opened).toHaveLength(1);
    expect(opened[0]?.id).toBe('pixel-forge');
  });

  test('pane render draws svg source with animations', async ($: any, on: any) => {
    stubUiCalls(on);
    mock.store(on, { count: 0 });

    const ui = await $.ui.mount({
      plugin: 'pixel-forge-dock',
      surface: SURFACE,
      component: 'Pane',
      props: {},
      requestId: 'pixel-forge',
    });
    const tree = await ui.drawn();
    const flat = JSON.stringify(tree);
    expect(flat).toContain('Svg');
    expect(flat).toContain('@keyframes pf-pulse');
    expect(flat).toContain('animateTransform');
    expect(flat).toContain('@keyframes pf-spin');
  });

  test('press increments counter and invalidates', async ($: any, on: any) => {
    stubUiCalls(on);
    mock.store(on, { count: 2 });

    const ui = await $.ui.mount({
      plugin: 'pixel-forge-dock',
      surface: SURFACE,
      component: 'Pane',
      props: {},
      requestId: 'pixel-forge',
    });

    await ui.press({ key: 'pf-inc' });
    expect(await ui.find({ text: 'count = 3' })).toBeTruthy();

    await ui.press({ key: 'pf-reset' });
    expect(await ui.find({ text: 'count = 0' })).toBeTruthy();
  });
});
