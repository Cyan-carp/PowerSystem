const { _electron } = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const run = require('../scripts/test-run-directory.cjs').testRunDirectory();
const report = { checks: [], limit: 'Real compiled UI in sandboxed Chromium. Browser, S01 method surface and incompatible protocol are simulated; no installed S01 or production session. No business writes or OS capability claims.' };
let app;
(async () => {
  for (const mode of ['browser', 's01', 'incompatible']) {
    await fetch('http://127.0.0.1:4175/__control/clear401', { method: 'POST' });
    const env = { ...process.env, S03_COMPAT_MODE: mode }; delete env.ELECTRON_RUN_AS_NODE;
    app = await _electron.launch({ executablePath: require('electron'), args: [path.join(__dirname, 's03-compat-bootstrap.cjs')], env });
    const page = await app.firstWindow();
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    if (mode !== 'browser') await page.addInitScript(({ mode }) => {
      window.powerSystemDesktop = { getCapabilities: async () => ({ protocol: mode === 's01' ? 'powersystem-desktop/v1' : 'unknown/v0', features: { notifications: false, preferences: false, saveAs: false, tray: false } }) };
    }, { mode });
    await page.goto('http://127.0.0.1:4175/'); await page.waitForURL(/\/login/);
    await page.fill('#username', 'A'); await page.fill('#password', 'synthetic'); await page.click('button[type=submit]');
    await page.waitForURL(/\/dashboard$/); await page.locator('.meter.ok').waitFor();
    assert.equal(await page.getByRole('button', { name: /告警音已/ }).count(), 1);
    for (const route of ['/reports', '/agent', '/agent/schedules']) {
      await page.goto('http://127.0.0.1:4175' + route); await page.locator('.meter.ok').waitFor();
    }
    await page.click('[aria-label="退出登录"]'); await page.waitForURL(/\/login/);
    assert.deepEqual(errors, []);
    report.checks.push({ mode, status: 'passed', scenarios: ['login', 'realtime', 'reports', 'agent', 'existing schedules', 'logout', 'no missing-method error'] });
    await app.close(); app = null;
  }
  report.status = 'passed';
})().catch(error => { report.status = 'failed'; report.error = error.stack; process.exitCode = 1; app?.process().kill(); })
  .finally(() => { fs.writeFileSync(path.join(run, 's03-compat-results.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2)); });
