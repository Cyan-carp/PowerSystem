const { test } = require('node:test');
const assert = require('node:assert/strict');
const { NotificationSession, validateAlarm } = require('../dist/notification-session');
const { allowedDownload, completeSize, safeFilename } = require('../dist/download-policy');
test('notification validation rejects unsafe IDs, types, extra keys and control/markup text', () => {
  const good = { alarmId: 42, level: 'urgent', title: '平台告警', body: '请核查' };
  assert.deepEqual(validateAlarm(good), good);
  for (const bad of [null, [], { ...good, alarmId: 0 }, { ...good, alarmId: Number.MAX_SAFE_INTEGER + 1 }, { ...good, level: 'critical' }, { ...good, title: '<script>' }, { ...good, body: '\u0000' }, { ...good, title: 'x'.repeat(101) }, { ...good, token: 'x' }]) assert.throws(() => validateAlarm(bad));
});
test('dedup is exact within an in-memory session and stale callbacks fail after reset', () => {
  const state = new NotificationSession(), old = state.generation;
  assert.equal(state.claim(42), true); assert.equal(state.claim(42), false); assert.equal(state.claim(43), true);
  state.reset(); assert.equal(state.current(old), false); assert.equal(state.claim(42), true);
});
test('download checks real initiator origin, legal blob origin and content type', () => {
  const origin = 'https://www.cyancarp.cn';
  assert.equal(allowedDownload(origin, `blob:${origin}/abc`, 'text/csv'), true);
  for (const args of [['https://evil.test', `blob:${origin}/abc`, 'text/csv'], [origin, 'blob:https://evil.test/abc', 'text/csv'], ['', `${origin}/file`, 'text/csv'], [origin, `${origin}/file`, 'text/html'], [origin, 'file:///tmp/a.csv', 'text/csv']]) assert.equal(allowedDownload(...args), false);
});
test('unknown length is accepted only when actual disk size matches received bytes', () => {
  assert.equal(completeSize(42, 0, 42), true); assert.equal(completeSize(42, 42, 42), true);
  assert.equal(completeSize(42, 43, 42), false); assert.equal(completeSize(42, 0, 41), false);
  assert.equal(safeFilename('../a\u0000.csv').includes('/'), false); assert.equal(safeFilename('CON.csv'), 'report.csv');
});
