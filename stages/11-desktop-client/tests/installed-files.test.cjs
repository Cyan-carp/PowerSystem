const { test } = require('node:test');
const assert = require('node:assert/strict');
const { resolveInstallDirectory } = require('../scripts/installed-files.cjs');
test('NSIS registration without InstallLocation resolves from validated DisplayIcon', () => {
  const directory = 'C:\\Users\\tester\\AppData\\Local\\Programs\\powersystem-desktop';
  assert.equal(resolveInstallDirectory({ InstallLocation: null, DisplayIcon: directory + '\\PowerSystem Desktop.exe,0' }), directory);
  assert.equal(resolveInstallDirectory({ DisplayIcon: '"' + directory + '\\PowerSystem Desktop.exe",-1' }), directory);
  assert.equal(resolveInstallDirectory({ InstallLocation: directory }), directory);
  for (const record of [{}, { DisplayIcon: 'https://example.com/PowerSystem Desktop.exe' }, { DisplayIcon: 'C:\\Other\\evil.exe,0' }]) assert.throws(() => resolveInstallDirectory(record));
});
