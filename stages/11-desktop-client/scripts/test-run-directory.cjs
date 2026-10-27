// Test/build tooling only. Never imported by the distributable application.
const path = require('node:path');
const root = path.resolve(__dirname, '../../../artifacts/v5-desktop');
function testRunDirectory(value = process.env.POWERSYSTEM_TEST_RUN_DIRECTORY) {
  const run = path.resolve(value || path.join(root, 's02-20261026-r1'));
  const relative = path.relative(root, run);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('Test evidence directory must be inside v5-desktop artifacts');
  return run;
}
module.exports = { testRunDirectory };
