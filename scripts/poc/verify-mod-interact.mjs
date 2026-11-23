import { createRequire } from 'module';
const require = createRequire('D:/tools/node-v24.19.0-win-x64/node_modules/@playwright/cli/node_modules/');
const { chromium } = require('playwright');

const URL = 'http://127.0.0.1:8322';
const browser = await chromium.launch({
  headless: true,
  executablePath: 'C:/Users/Yang Dawei/AppData/Local/ms-playwright/chromium-1148/chrome-win/chrome.exe',
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('request', (r) => {
  if (r.url().includes('_codebuddy.ai/')) console.log('[req]', r.url().split('_codebuddy.ai/')[1], JSON.stringify(r.postData() || '').slice(0, 200));
});
await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForSelector('textarea, [contenteditable="true"]', { timeout: 30000 });
await page.waitForTimeout(3000);

// 1) frame-diff the SVG to prove animation: two clips 800ms apart
const pane = page.locator('aside.mod-dock, [data-mod-dock]').first();
const box = await pane.boundingBox();
const clip = box ? { x: box.x, y: box.y, width: box.width, height: Math.min(box.height, 260) } : undefined;
const f1 = await page.screenshot({ clip });
await page.waitForTimeout(800);
const f2 = await page.screenshot({ clip });
const frameDiffers = !f1.equals(f2);
console.log(JSON.stringify({ frameDiffers, f1: f1.length, f2: f2.length }));

// 2) press the +1 button, then read the count text
const plusBtn = pane.getByText('+1', { exact: true }).first();
await plusBtn.click();
await page.waitForTimeout(1500);
const countText = await pane.innerText();
console.log('--- pane text after +1:');
console.log(countText.slice(0, 400));
const pressed = /count\s*=\s*1/.test(countText);
console.log(JSON.stringify({ pressedCount1: pressed }));
await page.screenshot({ path: 'mod-dock-after-press.png' });

// 3) reset
const resetBtn = pane.getByText('归零', { exact: true }).first();
await resetBtn.click();
await page.waitForTimeout(1500);
const text2 = await pane.innerText();
console.log(JSON.stringify({ resetOk: /count\s*=\s*0/.test(text2) }));

await browser.close();
