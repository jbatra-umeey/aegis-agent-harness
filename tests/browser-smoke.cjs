const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const { chromium } = require('playwright');

(async () => {
  const directory = fs.mkdtempSync(`${os.tmpdir()}/aegis-browser-`);
  const url = process.env.AEGIS_URL || 'http://127.0.0.1:8092';
  const server = process.env.AEGIS_URL ? null : spawn(process.env.AEGIS_PYTHON || 'python', ['-m', 'aegis', 'serve', '--port', '8092'], {
    env: { ...process.env, AEGIS_MODE: 'fixture', AEGIS_IDENTITIES_JSON: '{}', AEGIS_DATA_DIR: directory, AEGIS_LANGSMITH_SUMMARIES: '0' }, stdio: 'ignore',
  });
  let browser;
  try {
    for (let i = 0; i < 60; i++) {
      try { if ((await fetch(`${url}/healthz`)).ok) break; } catch {}
      if (i === 59) throw new Error('Local server did not become ready.');
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE || undefined, args: process.env.CHROMIUM_EXECUTABLE ? ['--no-sandbox', '--disable-dev-shm-usage'] : [] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(url); await page.waitForSelector('#scenario option', { state: 'attached' });
    await page.locator('#run-button').click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'awaiting approval');
    assert.equal(await page.locator('#approval').isVisible(), true);
    assert.equal(await page.locator('.evidence-card').count(), 3);
    assert.equal(await page.locator('#metric-calls').textContent(), '9');
    fs.mkdirSync('artifacts', { recursive: true });
    await page.screenshot({ path: 'dashboard.png', fullPage: true });
    await page.locator('#approve').click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'completed');
    assert.match(await page.locator('#simulation').textContent(), /420 → 280/);
    const downloadEvent = page.waitForEvent('download'); await page.locator('#download').click();
    assert.match((await downloadEvent).suggestedFilename(), /^aegis-.*\.json$/);
    await page.locator('#run-button').click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'awaiting approval');
    assert.equal(await page.locator('#metric-calls').textContent(), '0');
    assert.equal(await page.locator('#metric-cache').textContent(), '9');
    await page.locator('#decline').click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'declined');
    await page.locator('#scenario').selectOption('forbidden_tool'); await page.locator('#run-button').click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'blocked');
    assert.match(await page.locator('#proposal').textContent(), /tool_scope_denied/);
    await page.locator('#identity').selectOption('demo-operator-orion');
    await page.waitForFunction(() => document.querySelector('#history').textContent.includes('No reviews'));
    assert.equal(await page.locator('#approval').isVisible(), false);
    await page.locator('#scenario').selectOption('healthy');
    // Deliberately hostile model text verifies the UI rendering boundary independently of fixtures.
    await page.route('**/api/runs', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch(); const json = await response.json();
      json.proposal.summary = '<img src=x onerror="window.injected=true">';
      await route.fulfill({ response, json });
    });
    await page.locator('#run-button').click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'completed');
    assert.match(await page.locator('#proposal').textContent(), /<img/);
    assert.equal(await page.locator('#proposal img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: 'artifacts/dashboard-mobile.png', fullPage: true });
    await page.reload(); await page.waitForSelector('#identity option', { state: 'attached' });
    await page.locator('#identity').selectOption('demo-operator-orion');
    await page.waitForFunction(() => document.querySelector('#history').textContent.includes('healthy'));
    await page.locator('.history-item').first().click();
    await page.waitForFunction(() => document.querySelector('#outcome').textContent === 'completed');
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: approval, denial, cache reuse, tool boundary, tenant switch, export, hostile rendering, mobile layout, reload.');
  } finally {
    if (browser) await browser.close();
    if (server) server.kill('SIGTERM');
    fs.rmSync(directory, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
