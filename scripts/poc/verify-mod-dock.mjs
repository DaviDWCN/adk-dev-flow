import { createRequire } from 'module';
const require = createRequire('D:/tools/node-v24.19.0-win-x64/node_modules/@playwright/cli/node_modules/');
const { chromium } = require('playwright');

const URL = 'http://127.0.0.1:8322';
const out = process.argv[2] || 'mod-dock-shot.png';

const browser = await chromium.launch({
  headless: true,
  executablePath: 'C:/Users/Yang Dawei/AppData/Local/ms-playwright/chromium-1148/chrome-win/chrome.exe',
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleLines = [];
page.on('console', (m) => consoleLines.push(`[console.${m.type()}] ${m.text().slice(0, 300)}`));

await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForSelector('textarea, [contenteditable="true"]', { timeout: 30000 });
await page.waitForTimeout(2000);

// type a message and send
const input = page.locator('textarea, [contenteditable="true"]').first();
await input.click();
await input.fill('请只回复：开始');
await input.press('Enter');

// wait for the agent to reply
await page.waitForTimeout(15000);

const dockCount = await page.evaluate(
  () => document.querySelectorAll('aside.mod-dock,[data-mod-dock],.mod-root').length,
);
const bodyHasForge = await page.evaluate(() => document.body.innerHTML.includes('pixel-forge'));
console.log(JSON.stringify({ dockCount, bodyHasForge }, null, 2));
console.log('--- console lines mentioning mod/forge:');
for (const l of consoleLines) if (/mod|forge/i.test(l)) console.log(l);
console.log('--- total console lines:', consoleLines.length);
await page.screenshot({ path: out, fullPage: false });
await browser.close();
