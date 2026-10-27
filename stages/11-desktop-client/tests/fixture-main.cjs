// Test-only bootstrap. Excluded from all distributable packages.
const { app, session, ipcMain } = require('electron');
const path = require('node:path');
if (!process.env.POWERSYSTEM_TEST_DATA) throw new Error('Test data directory required');
app.setPath('userData', path.resolve(process.env.POWERSYSTEM_TEST_DATA));
const handlers = new Map();
const handle = ipcMain.handle.bind(ipcMain);
ipcMain.handle = (channel, listener) => { handlers.set(channel, listener); handle(channel, listener); };
globalThis.__s01Handlers = handlers;
app.whenReady().then(() => {
  const ses = session.fromPartition('powersystem-desktop-memory-v1', { cache: false });
  ses.protocol.handle('https', request => {
    if (process.env.POWERSYSTEM_TEST_OFFLINE === '1') return Response.error();
    const child = new URL(request.url).pathname === '/child';
    return new Response('<!doctype html><html><head><title>S01 isolated fixture</title></head><body><h1>Isolated test</h1>' + (child ? '' : '<a id="external" href="https://example.com/">external</a><iframe id="child" src="https://www.cyancarp.cn/child"></iframe>') + '</body></html>', { headers: { 'Content-Type': 'text/html' } });
  });
});
require('../dist/main.js');
