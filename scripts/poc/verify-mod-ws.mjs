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
  if (r.method() === 'POST') {
    const b = r.postData() || '';
    if (/modSlotPress|press/.test(b)) console.log('[http-post]', r.url(), b.slice(0, 400));
  }
});
page.on('response', async (r) => {
  if (r.request().method() === 'POST' && /modSlotPress|press/.test(r.request().postData() || '')) {
    try { console.log('[http-resp]', r.status(), (await r.text()).slice(0, 400)); } catch {}
  }
});
await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForSelector('textarea, [contenteditable="true"]', { timeout: 30000 });
await page.waitForTimeout(3000);

const btn = page.locator('button[data-mod-element="pf-inc"]').first();
console.log('button count:', await page.locator('button[data-mod-element]').count());
await btn.click();
await page.waitForTimeout(2500);
const pane = page.locator('aside.mod-dock, [data-mod-dock]').first();
const text = await pane.innerText();
console.log('--- pane text after click:');
console.log(text.slice(0, 300));
await browser.close();
