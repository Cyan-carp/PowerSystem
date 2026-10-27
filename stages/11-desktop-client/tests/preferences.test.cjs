const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { PreferencesStore, PreferencesError, validatePreferencesPatch } = require('../dist/preferences');

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ps-desktop-prefs-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function readFile(dir) {
  return JSON.parse(fs.readFileSync(path.join(dir, 'preferences.json'), 'utf8'));
}

function codeOf(fn) {
  try { fn(); } catch (error) { return error instanceof PreferencesError ? error.code : 'UNEXPECTED_ERROR'; }
  return 'NO_ERROR';
}

test('defaults are notifications on and launch at login off', t => {
  const dir = tempDir(t);
  const store = new PreferencesStore(dir);
  assert.deepEqual(store.get(), { notificationsEnabled: true, launchAtLogin: false });
  assert.equal(store.getTrayHintShown(), false);
  assert.equal(fs.existsSync(path.join(dir, 'preferences.json')), false, 'defaults do not write until the user changes something');
});

test('update persists exactly the two booleans and reloads', t => {
  const dir = tempDir(t);
  const store = new PreferencesStore(dir);
  assert.deepEqual(store.update({ notificationsEnabled: false }), { notificationsEnabled: false, launchAtLogin: false });
  assert.deepEqual(store.update({ launchAtLogin: true }), { notificationsEnabled: false, launchAtLogin: true });
  const raw = readFile(dir);
  assert.deepEqual(Object.keys(raw).sort(), ['launchAtLogin', 'notificationsEnabled', 'trayHintShown']);
  assert.equal(raw.notificationsEnabled, false);
  assert.equal(raw.launchAtLogin, true);
  assert.deepEqual(new PreferencesStore(dir).get(), { notificationsEnabled: false, launchAtLogin: true });
});

test('patch validation returns a fresh whitelisted object', () => {
  assert.deepEqual(validatePreferencesPatch({ notificationsEnabled: true }), { notificationsEnabled: true });
  assert.deepEqual(validatePreferencesPatch({ notificationsEnabled: false, launchAtLogin: true }), { notificationsEnabled: false, launchAtLogin: true });
});

test('non-object, empty and non-boolean patches are rejected without writing', t => {
  const dir = tempDir(t);
  const store = new PreferencesStore(dir);
  const bad = [
    null, undefined, true, 0, 'on', [], [true], () => true, new Date(),
    {}, { notificationsEnabled: 1 }, { launchAtLogin: 'true' }, { notificationsEnabled: null },
    { notificationsEnabled: true, launchAtLogin: 'yes' }, { notificationsEnabled: true, extra: false },
  ];
  for (const value of bad) {
    const code = codeOf(() => store.update(value));
    assert.ok(code === 'PREFERENCES_PATCH_INVALID' || code === 'PREFERENCES_PATCH_EMPTY'
      || code === 'PREFERENCES_PATCH_KEY_DENIED' || code === 'PREFERENCES_PATCH_VALUE_INVALID', String(code));
  }
  assert.deepEqual(store.get(), { notificationsEnabled: true, launchAtLogin: false });
  assert.equal(fs.existsSync(path.join(dir, 'preferences.json')), false);
});

test('prototype, inherited, symbol and hidden keys are rejected', t => {
  const store = new PreferencesStore(tempDir(t));
  const fromJson = JSON.parse('{"__proto__":{"notificationsEnabled":false}}');
  const withConstructor = { constructor: true };
  const symbolKey = { [Symbol('notificationsEnabled')]: true };
  const hidden = Object.defineProperty({}, 'launchAtLogin', { value: true, enumerable: false });
  const inherited = Object.create({ notificationsEnabled: false });
  assert.equal(codeOf(() => store.update(fromJson)), 'PREFERENCES_PATCH_KEY_DENIED');
  assert.equal(codeOf(() => store.update(withConstructor)), 'PREFERENCES_PATCH_KEY_DENIED');
  for (const value of [symbolKey, hidden, inherited]) {
    assert.equal(codeOf(() => store.update(value)), 'PREFERENCES_PATCH_INVALID', JSON.stringify(Object.keys(value)));
  }
  assert.deepEqual(store.get(), { notificationsEnabled: true, launchAtLogin: false });
  assert.equal(Object.prototype.notificationsEnabled, undefined);
});

test('a failed write keeps the previous in-memory values', t => {
  const dir = tempDir(t);
  const store = new PreferencesStore(dir);
  fs.mkdirSync(path.join(dir, 'preferences.json'));
  assert.equal(codeOf(() => store.update({ notificationsEnabled: false })), 'PREFERENCES_WRITE_FAILED');
  assert.deepEqual(store.get(), { notificationsEnabled: true, launchAtLogin: false });
});

test('tray hint is a separate flag that persists and stays out of get()', t => {
  const dir = tempDir(t);
  const store = new PreferencesStore(dir);
  store.markTrayHintShown();
  store.markTrayHintShown();
  assert.equal(store.getTrayHintShown(), true);
  assert.deepEqual(Object.keys(store.get()).sort(), ['launchAtLogin', 'notificationsEnabled']);
  const reopened = new PreferencesStore(dir);
  assert.equal(reopened.getTrayHintShown(), true);
  reopened.update({ notificationsEnabled: false });
  assert.equal(new PreferencesStore(dir).getTrayHintShown(), true, 'preference update must not clear the hint');
  assert.equal(readFile(dir).trayHintShown, true);
});

test('corrupt files fall back to defaults and sensitive extras are never rewritten', t => {
  const dir = tempDir(t);
  fs.writeFileSync(path.join(dir, 'preferences.json'), 'not json');
  assert.deepEqual(new PreferencesStore(dir).get(), { notificationsEnabled: true, launchAtLogin: false });
  fs.writeFileSync(path.join(dir, 'preferences.json'), JSON.stringify('text'));
  assert.deepEqual(new PreferencesStore(dir).get(), { notificationsEnabled: true, launchAtLogin: false });
  fs.writeFileSync(path.join(dir, 'preferences.json'), JSON.stringify({ notificationsEnabled: 'no', launchAtLogin: true, token: 'secret', path: 'C:\\data' }));
  const store = new PreferencesStore(dir);
  assert.deepEqual(store.get(), { notificationsEnabled: true, launchAtLogin: true });
  store.update({ launchAtLogin: false });
  const raw = readFile(dir);
  assert.deepEqual(Object.keys(raw).sort(), ['launchAtLogin', 'notificationsEnabled', 'trayHintShown']);
  assert.equal(fs.readFileSync(path.join(dir, 'preferences.json'), 'utf8').includes('secret'), false);
});

test('constructor rejects an unusable user data directory', () => {
  for (const value of ['', 'x'.repeat(4097), 'bad\u0000path', null, 42]) {
    assert.equal(codeOf(() => new PreferencesStore(value)), 'PREFERENCES_PATH_INVALID', String(value));
  }
});
