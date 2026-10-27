// Separate compile-time configuration. Never select an origin at application runtime.
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const run = require('./test-run-directory.cjs').testRunDirectory();
const output = path.join(run, 'isolated-app');
fs.mkdirSync(output, { recursive: true });
const tsc = spawnSync(process.execPath, [path.join(root, 'node_modules/typescript/bin/tsc'), '--outDir', path.join(output, 'dist')], { cwd: root, stdio: 'inherit' });
if (tsc.status !== 0) process.exit(tsc.status || 1);
for (const name of ['policy.js', 'preload.js']) {
  const target = path.join(output, 'dist', name);
  const source = fs.readFileSync(target, 'utf8');
  if (!source.includes('https://www.cyancarp.cn')) throw new Error('Expected source origin missing: ' + name);
  const isolated = source.replaceAll('https://www.cyancarp.cn', 'http://127.0.0.1:4175');
  if (isolated.includes('https://www.cyancarp.cn') || !isolated.includes('http://127.0.0.1:4175')) throw new Error('Isolated source invariant failed');
  fs.writeFileSync(target, isolated);
}
const mainFile = path.join(output, 'dist/main.js');
fs.writeFileSync(mainFile, fs.readFileSync(mainFile, 'utf8').replaceAll('PowerSystem Desktop', 'PowerSystem Desktop S02 Test').replaceAll('cn.cyancarp.powersystem.desktop', 'cn.cyancarp.powersystem.desktop.s02test'));
fs.mkdirSync(path.join(output, 'src'), { recursive: true });
fs.copyFileSync(path.join(root, 'src/fallback.html'), path.join(output, 'src/fallback.html'));
fs.writeFileSync(path.join(output, 'package.json'), JSON.stringify({ name: 'powersystem-desktop-s02-isolated', productName: '源网智联 S02 隔离验收', version: '0.2.0', main: 'dist/main.js', private: true }));
console.log(output);
