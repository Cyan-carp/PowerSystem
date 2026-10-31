const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const asar = require('@electron/asar');
const { getCurrentFuseWire, FuseV1Options } = require('@electron/fuses');
const yaml = require('js-yaml');
const run = path.resolve(process.argv[2] || '../../artifacts/v5-桌面客户端/S02-运维桌面-20261026-r1');
const unpacked = path.join(run, 'package/win-unpacked');
const exe = path.join(unpacked, 'PowerSystem Desktop.exe');
const hash = target => crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex');
const report = { status: 'running', kind: 'unpacked-artifact-static-and-startup-only', checks: [] };
let child;
(async () => {
  const config = yaml.load(fs.readFileSync('electron-builder.yml', 'utf8'));
  assert.equal(config.nsis.oneClick, true); assert.equal(config.nsis.perMachine, false);
  assert.equal(config.nsis.allowElevation, false); assert.equal(config.nsis.packElevateHelper, false);
  assert.equal(config.win.requestedExecutionLevel, 'asInvoker');
  assert.equal(config.nsis.createDesktopShortcut, true); assert.equal(config.nsis.createStartMenuShortcut, true);
  assert.equal(config.asar, true); assert.equal(config.publish, null);
  assert.deepEqual(config.win.target, [{ target: 'nsis', arch: ['x64'] }]);
  report.checks.push('current-user installer and packaging safety configuration');
  const bytes = fs.readFileSync(exe); const pe = bytes.readUInt32LE(0x3c);
  assert.equal(bytes.readUInt16LE(pe + 4), 0x8664); report.checks.push('PE x64');
  const archive = path.join(unpacked, 'resources/app.asar');
  const files = asar.listPackage(archive).filter(name => !asar.statFile(archive, name.replace(/^[/\\]/, '')).files);
  assert.ok(files.every(name => /^[/\\](package\.json|dist[/\\][\w-]+\.js)$/.test(name)), JSON.stringify(files));
  const metadata = JSON.parse(asar.extractFile(archive, 'package.json'));
  assert.equal(metadata.version, '0.2.0'); assert.equal(metadata.main, 'dist/main.js');
  for (const name of files.filter(name => /\\.js$/.test(name))) { const content = asar.extractFile(archive, name.replace(/^[/\\\\]/, '')).toString(); assert.ok(!content.includes('127.0.0.1:4175') && !content.includes('S02 Test'), name); }
  const compiled = asar.extractFile(archive, 'dist/main.js');
  assert.equal(crypto.createHash('sha256').update(compiled).digest('hex'), hash(path.resolve('dist/main.js')));
  assert.equal(hash(path.join(unpacked, 'resources/fallback.html')), hash(path.resolve('src/fallback.html')));
  assert.equal(fs.existsSync(path.join(unpacked, 'resources/elevate.exe')), false);
  report.files = files; report.checks.push('asar whitelist and compiled main/fallback match source; no elevate helper');
  const fuses = await getCurrentFuseWire(exe);
  assert.equal(fuses[FuseV1Options.RunAsNode], 48);
  assert.equal(fuses[FuseV1Options.EnableNodeOptionsEnvironmentVariable], 48);
  assert.equal(fuses[FuseV1Options.EnableNodeCliInspectArguments], 48);
  assert.equal(fuses[FuseV1Options.EnableEmbeddedAsarIntegrityValidation], 49);
  assert.equal(fuses[FuseV1Options.OnlyLoadAppFromAsar], 49);
  report.fuses = fuses; report.checks.push('production fuses verified from executable');
  const data = path.join(run, 'packaged-offline-user-data-' + Date.now());
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  child = spawn(exe, [`--user-data-dir=${data}`, '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>'], { env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  for (const stream of [child.stdout, child.stderr]) stream.on('data', value => fs.appendFileSync(path.join(run, 'packaged-smoke-stderr.txt'), value));
  let audit = [];
  for (let count = 0; count < 200; count++) {
    const log = path.join(data, 'security-events.jsonl');
    if (fs.existsSync(log)) audit = fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
    if (audit.some(entry => entry.code === 'startup-load-failed')) { await new Promise(resolve => setTimeout(resolve, 1500)); break; }
    if (child.exitCode !== null) throw new Error('Packaged process exited before fallback startup');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(audit.some(entry => entry.code === 'startup-load-failed'));
  assert.ok(!audit.some(entry => entry.code === 'fallback-load-failed' || entry.code === 'startup-fatal'));
  assert.equal(child.exitCode, null);
  report.audit = audit; report.checks.push('hardened unpacked executable stays running on forced startup failure');
  report.limit = 'No installation or owner clicks. Unreachable local proxy applies only to this process. Process terminated by harness; not a normal-close acceptance. Fallback visual/buttons verified separately in fixture and awaiting installed owner test.';
  child.kill(); child = null;
  report.sha256 = {};
  for (const relative of ['PowerSystem Desktop.exe', 'resources/app.asar', 'resources/fallback.html']) report.sha256[relative] = hash(path.join(unpacked, relative));
  report.status = 'passed';
})().catch(error => { report.status = 'failed'; report.error = error.stack; process.exitCode = 1; if (child) child.kill(); }).finally(() => { fs.writeFileSync(path.join(run, 'package-smoke.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2)); });

