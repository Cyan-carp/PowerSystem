const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { testRunDirectory } = require('../scripts/test-run-directory.cjs');
test('test evidence relocation rejects artifact root and traversal outside its boundary', () => {
  const root = path.resolve(__dirname, '../../../artifacts/v5-桌面客户端');
  assert.equal(testRunDirectory(path.join(root, 's03-20261027-r1')), path.join(root, 's03-20261027-r1'));
  for (const value of [root, path.join(root, '..', 'other'), root + '-outside/run']) assert.throws(() => testRunDirectory(value));
});
