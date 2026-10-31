const { _electron } = require('playwright-core');
const { createStartupProxy } = require('../scripts/startup-proxy.cjs');
const { once } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const run = path.resolve('../../artifacts/v5-桌面客户端/S01-基础安装版-20261026-r1');
const proxy = createStartupProxy();
let app;
const report = { kind: 'test-only-proxy-with-real-fixed-HTTPS-no-login', status: 'running' };
(async () => {
  proxy.server.listen(0, '127.0.0.1'); await once(proxy.server, 'listening');
  const env = { ...process.env, POWERSYSTEM_TEST_DATA: path.join(run, 'owner-proxy-technical-profile') };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await _electron.launch({ executablePath: require('electron'), args: [path.join(__dirname, 'startup-owner-bootstrap.cjs'), `--proxy-server=http://127.0.0.1:${proxy.server.address().port}`, '--proxy-bypass-list=<-loopback>'], env });
  const page = await app.firstWindow(); await page.waitForURL(/fallback\.html$/);
  report.localPage = true;
  proxy.enable(); await page.click('#retry');
  await page.waitForURL(url => url.origin === 'https://www.cyancarp.cn', { timeout: 40000 });
  await page.waitForLoadState('domcontentloaded');
  const capabilities = await page.evaluate(() => window.powerSystemDesktop.getCapabilities());
  assert.equal(capabilities.protocol, 'powersystem-desktop/v1');
  report.fixedHttpsAfterRetry = true;
  report.status = 'passed';
})().catch(error => { report.status = 'failed'; report.error = error.stack; process.exitCode = 1; }).finally(async () => {
  if (app) await app.close().catch(() => {}); proxy.close();
  fs.writeFileSync(path.join(run, 'owner-proxy-technical.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2));
});
