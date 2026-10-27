const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const artifactRoot = path.resolve(__dirname, '../../../artifacts/v5-desktop');
const runArg = process.argv.indexOf('--run-directory');
const run = path.resolve(runArg >= 0 ? process.argv[runArg + 1] : path.join(artifactRoot, 's02-20261026-r1'));
if (!run.startsWith(artifactRoot + path.sep)) throw new Error('Unexpected run directory.');
async function ready() {
  try { const data = await (await fetch('http://127.0.0.1:4175/__state', { signal: AbortSignal.timeout(1500) })).json(); if (!data.artifact || data.artifact.runID !== 's02-synthetic-run' || data.batch !== path.basename(run)) throw new Error('Port 4175 belongs to another service or test batch.'); return true; }
  catch (error) { if (String(error).includes('belongs')) throw error; return false; }
}
(async () => {
  if (!(await ready())) {
    const log = fs.openSync(path.join(run, 'owner-fixture-console.txt'), 'a');
    const child = spawn(process.execPath, [path.join(__dirname, 's02-fixture.cjs'), '--run-directory', run], { cwd: path.resolve(__dirname, '..'), windowsHide: true, detached: true, stdio: ['ignore', log, log] });
    child.unref();
    for (let n = 0; n < 30; n++) { if (await ready()) break; await new Promise(r => setTimeout(r, 200)); }
    if (!(await ready())) throw new Error('Fixture did not start. Keep this window open and report the error.');
  }
  console.log('S02 isolated service is ready. No production proxy or system network changes.');
  console.log('Test accounts: A / B. Password: synthetic (never enter real credentials).');
  console.log('Open the control page, then install the S02 Isolated package.');
  console.log('Control: http://127.0.0.1:4175/__control');
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
