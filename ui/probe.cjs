
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text().slice(0, 300)); });
  await page.goto('http://localhost:4573/#/personas', { waitUntil: 'networkidle' }).catch(e => errors.push('NAV: ' + e.message));
  await page.waitForTimeout(1500);
  const heading = await page.locator('h1, h2').first().textContent().catch(() => '(none)');
  console.log(JSON.stringify({ heading, errors: errors.slice(0, 5) }, null, 1));
  await browser.close();
})();
