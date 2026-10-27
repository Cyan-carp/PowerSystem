const { test } = require('node:test');
const assert = require('node:assert/strict');
const { isBusinessUrl, isExternalHttps, isBusinessCaller, isFallbackCaller } = require('../dist/policy');
const { restoreState } = require('../dist/window-state');

test('origin rejects scheme, port, lookalike hosts and credentials', () => {
  assert.equal(isBusinessUrl('https://www.cyancarp.cn/alarms?id=1'), true);
  for (const value of ['http://www.cyancarp.cn', 'https://www.cyancarp.cn:444', 'https://www.cyancarp.cn.evil.test', 'https://cyancarp.cn', 'https://user@www.cyancarp.cn', 'file:///c:/test', 'javascript:alert(1)', 'data:text/html,test', 'invalid']) assert.equal(isBusinessUrl(value), false, value);
});
test('IPC requires owned window AND top frame AND current trusted document', () => {
  const ok = { owned: true, top: true, frameUrl: 'https://www.cyancarp.cn/login', currentUrl: 'https://www.cyancarp.cn/login' };
  assert.equal(isBusinessCaller(ok), true);
  for (const change of [{ owned: false }, { top: false }, { frameUrl: 'https://example.com' }, { currentUrl: 'file:///fallback.html' }]) assert.equal(isBusinessCaller({ ...ok, ...change }), false);
  const local = { ...ok, frameUrl: 'file:///app/fallback.html', currentUrl: 'file:///app/fallback.html' };
  assert.equal(isFallbackCaller(local, 'file:///app/fallback.html'), true);
  assert.equal(isFallbackCaller({ ...local, top: false }, 'file:///app/fallback.html'), false);
  assert.equal(isFallbackCaller(local, 'file:///evil/fallback.html'), false);
});
test('external browser accepts only bounded HTTPS with no embedded credentials', () => {
  assert.equal(isExternalHttps('https://example.com/path'), true);
  for (const value of ['http://example.com', 'file:///c:/test', 'javascript:alert(1)', 'ms-settings:privacy', 'https://user:secret@example.com', 'https://www.cyancarp.cn', 'https://example.com/' + 'a'.repeat(4096), null, {}]) assert.equal(isExternalHttps(value), false);
});
test('window geometry keeps a valid secondary display and recovers after removal', () => {
  const a = { x: 0, y: 0, width: 1920, height: 1040 };
  const b = { x: -1600, y: 0, width: 1600, height: 900 };
  const saved = { bounds: { x: -1500, y: 20, width: 1200, height: 800 }, maximized: true };
  assert.deepEqual(restoreState(saved, [a, b]), saved);
  const fixed = restoreState(saved, [a]);
  assert.ok(fixed.bounds.x >= 0 && fixed.bounds.x + fixed.bounds.width <= 1920);
  assert.equal(fixed.maximized, true);
});
test('corrupt, oversized and invalid state restore fully inside small work area', () => {
  const area = { x: 40, y: 30, width: 700, height: 500 };
  for (const value of [null, 'bad', {}, { bounds: { x: NaN, y: 0, width: 999999, height: -1 } }, { bounds: { x: 0, y: 0, width: 5000, height: 4000 } }]) {
    const { bounds } = restoreState(value, [area]);
    assert.ok(bounds.x >= 40 && bounds.y >= 30 && bounds.x + bounds.width <= 740 && bounds.y + bounds.height <= 530);
  }
});
