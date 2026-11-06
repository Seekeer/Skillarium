// Drives a running app started by verify-packaged.ps1 -DebugPort: opens the settings, runs a
// real update check and saves a screenshot of every state it passes through.
//   node app/scripts/drive-settings.mjs <port> <folder for screenshots>
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const [port = '9333', out = '.'] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
const page = browser.contexts()[0].pages()[0];
const shot = async (name) => {
  const file = path.join(out, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(`${name}: ${await page.getByTestId('update-summary').textContent().catch(() => '')} -> ${file}`);
};
await page.getByRole('button', { name: /Настройки/ }).click();
const summary = page.getByTestId('update-summary');
await summary.waitFor();
await shot('1-settings-opened');
// When the start-up check is over and nothing waits for a restart, check again by hand.
const check = page.getByRole('button', { name: 'Проверить обновления' });
await page.waitForFunction(() => !/Проверяю/.test(document.querySelector('[data-testid="update-summary"]')?.textContent || ''));
if (await check.isEnabled().catch(() => false)) {
  await check.click();
  await shot('2-checking');
}
// The app checked on start; the window follows that check to its end.
const deadline = Date.now() + 10 * 60 * 1000;
let last = '';
while (Date.now() < deadline) {
  const text = await summary.textContent();
  if (text !== last && /Загружаю версию [\d.]+ — \d+%/.test(text) && !last.includes('—')) await shot('2-downloading');
  if (/Проверяю/.test(text)) { await page.waitForTimeout(500); continue; }
  last = text;
  if (/загружена|последняя версия|не удалось/i.test(text)) break;
  await page.waitForTimeout(500);
}
await shot('3-result');
await browser.close();
