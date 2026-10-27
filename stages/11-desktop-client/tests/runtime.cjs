// Technical checks only. These do not replace the owner's installed-package tests.
const { _electron } = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const executablePath = require('electron');
const run = path.resolve(process.argv[2] || '../../artifacts/v5-desktop/s01-20261026-r1');
const data = path.join(run, 'runtime-user-data');
const marker = 'S01_SYNTHETIC_SESSION_MARKER_7c94';
const report = { kind: 'isolated-fixture-not-installed-package', checks: [], data };
let application;
function pass(name, detail) { report.checks.push({ name, status: 'passed', detail }); }
async function launch(offline = false) {
  const env = { ...process.env, POWERSYSTEM_TEST_DATA: data, POWERSYSTEM_TEST_OFFLINE: offline ? '1' : '0' };
  delete env.ELECTRON_RUN_AS_NODE;
  application = await _electron.launch({ executablePath, args: [path.join(__dirname, 'fixture-main.cjs')], env });
  application.process().stderr.on('data', value => fs.appendFileSync(path.join(run, 'runtime-stderr.txt'), value));
  const page = await application.firstWindow();
  await page.waitForLoadState('domcontentloaded');
  await application.evaluate(async ({ BrowserWindow }) => {
    for (let count = 0; count < 100; count++) {
      if (BrowserWindow.getAllWindows()[0]?.isVisible()) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error('Window did not become visible');
  });
  return page;
}
async function closeViaWindow() {
  const processRef = application.process();
  const exit = new Promise(resolve => processRef.once('exit', (code, signal) => resolve({ code, signal })));
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  const result = await Promise.race([exit, new Promise((_, reject) => setTimeout(() => reject(new Error('Window close left process running')), 10000))]);
  application = null;
  assert.equal(result.code, 0); return result;
}
(async () => {
  fs.mkdirSync(run, { recursive: true });
  let page = await launch();
  await page.waitForURL('https://www.cyancarp.cn/');
  const capabilities = await page.evaluate(() => window.powerSystemDesktop.getCapabilities());
  assert.equal(capabilities.protocol, 'powersystem-desktop/v1');
  assert.ok(Object.values(capabilities.features).every(value => value === false));
  pass('handshake', capabilities);
  const web = await application.evaluate(({ BrowserWindow }) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const p = wc.getLastWebPreferences();
    return { sandbox: p.sandbox, contextIsolation: p.contextIsolation, nodeIntegration: p.nodeIntegration, webSecurity: p.webSecurity, persistent: wc.session.isPersistent(), storagePath: wc.session.storagePath };
  });
  assert.equal(web.sandbox, true); assert.equal(web.contextIsolation, true); assert.equal(web.nodeIntegration, false); assert.equal(web.webSecurity, true); assert.equal(web.persistent, false); assert.equal(web.storagePath, null);
  assert.deepEqual(await page.evaluate(() => ({ require: typeof window.require, process: typeof window.process, ipc: typeof window.ipcRenderer })), { require: 'undefined', process: 'undefined', ipc: 'undefined' });
  pass('security-webpreferences-and-node-isolation', web);
  const child = page.frames().find(frame => frame !== page.mainFrame());
  await child.waitForLoadState('domcontentloaded');
  assert.equal(await child.evaluate(() => typeof window.powerSystemDesktop), 'undefined');
  const deny = await application.evaluate(async ({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    const h = globalThis.__s01Handlers.get('desktop:capabilities');
    const sub = w.webContents.mainFrame.frames[0];
    let subDenied = false, wrongWindowDenied = false, argsDenied = false;
    try { await h({ sender: w.webContents, senderFrame: sub }); } catch { subDenied = true; }
    const other = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
    try { await h({ sender: other.webContents, senderFrame: other.webContents.mainFrame }); } catch { wrongWindowDenied = true; }
    other.destroy();
    try { await h({ sender: w.webContents, senderFrame: w.webContents.mainFrame }, 'unexpected'); } catch { argsDenied = true; }
    return { subDenied, wrongWindowDenied, argsDenied };
  });
  assert.ok(Object.values(deny).every(Boolean)); pass('actual-ipc-denies-subframe-other-window-and-arguments', deny);
  await page.evaluate(() => { window.open('https://example.com/'); });
  assert.equal(await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
  pass('popup-denied');
  // Stub only OS browser dispatch, then use a real trusted automation click through production preload.
  await application.evaluate(({ shell }) => { globalThis.__external = []; shell.openExternal = async url => { globalThis.__external.push(url); }; });
  await page.click('#external');
  assert.deepEqual(await application.evaluate(() => globalThis.__external), ['https://example.com/']);
  pass('trusted-https-click-dispatches-system-browser', 'OS dispatch stubbed; actual browser appearance awaits owner');
  const boundaries = await application.evaluate(async ({ app, BrowserWindow, ipcMain }) => {
    const w = BrowserWindow.getAllWindows()[0];
    const h = globalThis.__s01Handlers.get('desktop:capabilities');
    let prevented = false, accepted = true;
    app.emit('certificate-error', { preventDefault: () => { prevented = true; } }, w.webContents, 'https://www.cyancarp.cn/', 'net::ERR_CERT_AUTHORITY_INVALID', {}, value => { accepted = value; });
    const event = { sender: w.webContents, senderFrame: w.webContents.mainFrame };
    for (const url of ['file:///C:/test.exe', 'javascript:alert(1)', 'ms-settings:privacy', 'http://example.com/']) ipcMain.emit('desktop:external-click', event, url);
    await w.loadURL('https://example.com/');
    let untrustedDenied = false;
    try { await h({ sender: w.webContents, senderFrame: w.webContents.mainFrame }); } catch { untrustedDenied = true; }
    const bridgeAbsent = await w.webContents.executeJavaScript('typeof window.powerSystemDesktop === "undefined"');
    await w.loadURL('https://www.cyancarp.cn/');
    return { certificateRejected: prevented && !accepted, untrustedDenied, bridgeAbsent, externalCount: globalThis.__external.length };
  });
  assert.equal(boundaries.certificateRejected, true); assert.equal(boundaries.untrustedDenied, true); assert.equal(boundaries.bridgeAbsent, true); assert.equal(boundaries.externalCount, 1);
  pass('certificate-handler-rejects-and-real-untrusted-document-has-no-bridge', boundaries);
  const probe = path.join(run, 'file-probe.html');
  fs.writeFileSync(probe, '<!doctype html><title>S01 non-allowed local file</title>');
  const fileUrl = require('node:url').pathToFileURL(probe).href;
  const fileDenied = await application.evaluate(async ({ BrowserWindow }, url) => {
    const w = BrowserWindow.getAllWindows()[0];
    let denied = false;
    try { await w.loadURL(url); } catch { denied = true; }
    await w.loadURL('https://www.cyancarp.cn/');
    return denied;
  }, fileUrl);
  assert.equal(fileDenied, true); pass('file-request-filter-blocks-non-fallback-local-file');
  await page.evaluate(() => { location.href = 'https://example.com/'; });
  await page.waitForTimeout(250); assert.equal(page.url(), 'https://www.cyancarp.cn/');
  pass('cross-origin-navigation-denied');
  const secondaryEnv = { ...process.env, POWERSYSTEM_TEST_DATA: data };
  delete secondaryEnv.ELECTRON_RUN_AS_NODE;
  const secondary = spawn(executablePath, [path.join(__dirname, 'fixture-main.cjs')], { env: secondaryEnv, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  secondary.stdout.on('data', value => fs.appendFileSync(path.join(run, 'secondary-process.txt'), value));
  secondary.stderr.on('data', value => fs.appendFileSync(path.join(run, 'secondary-process.txt'), value));
  const secondCode = await Promise.race([new Promise(resolve => secondary.once('exit', resolve)), new Promise((_, reject) => setTimeout(() => { secondary.kill(); reject(new Error('Second instance did not exit')); }, 10000))]);
  assert.equal(secondCode, 0);
  assert.equal(await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
  pass('second-launch-exits-and-preserves-one-window');
  const desired = await application.evaluate(({ BrowserWindow, screen }) => {
    const area = screen.getPrimaryDisplay().workArea;
    const bounds = { x: area.x + 10, y: area.y + 10, width: Math.min(980, area.width - 20), height: Math.min(700, area.height - 20) };
    BrowserWindow.getAllWindows()[0].setBounds(bounds); return bounds;
  });
  await page.evaluate(async value => {
    localStorage.setItem('probe', value); sessionStorage.setItem('probe', value); document.cookie = `probe=${value}; Secure; SameSite=Strict`;
    await new Promise((resolve, reject) => { const req = indexedDB.open('s01-probe'); req.onupgradeneeded = () => req.result.createObjectStore('values'); req.onerror = reject; req.onsuccess = () => { const db = req.result; const tx = db.transaction('values', 'readwrite'); tx.objectStore('values').put(value, 'probe'); tx.oncomplete = () => { db.close(); resolve(); }; }; });
  }, marker);
  await page.screenshot({ path: path.join(run, 'runtime-fixture.png') });
  const observedBounds = await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getNormalBounds());
  pass('window-close-exits', await closeViaWindow());
  const savedBounds = JSON.parse(fs.readFileSync(path.join(data, 'window-state.json'), 'utf8')).bounds;
  assert.deepEqual(savedBounds, observedBounds);
  page = await launch(); await page.waitForURL('https://www.cyancarp.cn/');
  const clean = await page.evaluate(async () => ({ local: localStorage.getItem('probe'), session: sessionStorage.getItem('probe'), cookies: document.cookie, databases: await indexedDB.databases() }));
  assert.equal(clean.local, null); assert.equal(clean.session, null); assert.equal(clean.cookies, ''); assert.equal(clean.databases.length, 0);
  pass('all-four-browser-stores-empty-after-process-restart', clean);
  const restored = await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getNormalBounds());
  assert.deepEqual(restored, savedBounds); pass('window-bounds-restored', { requested: desired, actualBeforeClose: observedBounds, saved: savedBounds, restored });
  await closeViaWindow();
  const hits = [];
  function scan(dir) { for (const entry of fs.readdirSync(dir, { withFileTypes: true })) { const target = path.join(dir, entry.name); if (entry.isDirectory()) scan(target); else if (fs.readFileSync(target).includes(Buffer.from(marker))) hits.push(path.relative(data, target)); } }
  scan(data); assert.deepEqual(hits, []); pass('synthetic-session-marker-absent-from-user-data', { filesWithMarker: hits });
  page = await launch(true);
  await page.waitForURL(/fallback\.html$/);
  assert.equal(await page.evaluate(() => typeof window.powerSystemDesktop), 'undefined');
  assert.equal(await page.locator('#retry').count(), 1);
  await page.screenshot({ path: path.join(run, 'runtime-fallback.png') });
  // Flip only isolated fixture response, then retry using the real local page button.
  await application.evaluate(() => { process.env.POWERSYSTEM_TEST_OFFLINE = '0'; });
  await page.click('#retry'); await page.waitForURL('https://www.cyancarp.cn/');
  pass('offline-local-page-and-retry-recover');
  await closeViaWindow();
  page = await launch(true); await page.waitForURL(/fallback\.html$/);
  const ref = application.process(); const exit = new Promise(resolve => ref.once('exit', resolve));
  await page.click('#exit'); assert.equal(await exit, 0); application = null;
  pass('local-exit-button-terminates-process');
})().then(() => { report.status = 'passed'; }).catch(async error => {
  report.status = 'failed'; report.error = error.stack; process.exitCode = 1;
  if (application) await application.close().catch(() => {});
}).finally(() => { fs.writeFileSync(path.join(run, 'runtime-results.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2)); });
