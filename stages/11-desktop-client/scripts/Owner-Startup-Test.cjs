// Interactive owner fixture. Never ships in the desktop installer.
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
const { createStartupProxy } = require('./startup-proxy.cjs');
const { resolveInstallDirectory } = require('./installed-files.cjs');
const run = path.resolve(__dirname, '../../../artifacts/v5-桌面客户端/S01-基础安装版-20261026-r1');
const proxy = createStartupProxy();
const consoleInput = readline.createInterface({ input: process.stdin, output: process.stdout });
let launcher;
let timer;
let stopping = false;
const shutdown = () => {
  if (stopping) return;
  stopping = true; clearTimeout(timer);
  if (launcher && launcher.exitCode === null) launcher.kill();
  proxy.close(); consoleInput.close();
};
function installedExecutable() {
  // Fixed read-only Windows query; no script execution policy or system setting changes.
  const query = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [pscustomobject]@{registration=@(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*' | Where-Object {$_.DisplayName -eq '源网智联'} | Select-Object InstallLocation,DisplayIcon);running=@(Get-Process -Name 'PowerSystem Desktop' -ErrorAction SilentlyContinue).Count} | ConvertTo-Json -Compress";
  const status = JSON.parse(execFileSync('powershell.exe', ['-NoProfile', '-Command', query], { encoding: 'utf8', windowsHide: true }));
  if (status.registration.length !== 1) throw new Error('请先安装本轮源网智联候选，再运行测试。');
  if (status.running) throw new Error('请先关闭所有源网智联窗口，再运行测试。');
  const directory = resolveInstallDirectory(status.registration[0]);
  const exe = path.join(directory, 'PowerSystem Desktop.exe');
  const expected = {
    'PowerSystem Desktop.exe': '13934a4456bb75016b8bd025fb2b4857901c99be16c26f04fbb9225a4c52eeac',
    'resources/app.asar': '5c8ecc290b7fea768191a2bb9399cbdef3e92bb3c348a9dc0fd7e3f659348268',
    'resources/fallback.html': '761a6dfeabd0e46a8004e6b01e8b8c9749d348dc3f6019c8ed0d9de00f7ce39f'
  };
  for (const [relative, digest] of Object.entries(expected)) {
    if (crypto.createHash('sha256').update(fs.readFileSync(path.join(directory, relative))).digest('hex') !== digest) throw new Error('已安装文件与本轮候选摘要不同，请反馈，暂停测试。');
  }
  return exe;
}
let exe;
try { exe = installedExecutable(); } catch (error) { console.error(error.message); consoleInput.close(); process.exitCode = 1; }
if (exe) proxy.server.listen(0, '127.0.0.1', () => {
  fs.mkdirSync(run, { recursive: true });
  const port = proxy.server.address().port;
  const info = path.join(run, 'owner-startup-proxy.json');
  fs.writeFileSync(info, JSON.stringify({ port }));
  console.log('电脑网络保持连接，系统代理设置保持原样。');
  console.log('1. 客户端应显示“暂时无法连接平台”。');
  console.log('2. 看见说明页后，回到本窗口按一次回车。');
  console.log('3. 再回客户端点击“重试连接”，应出现平台登录页。');
  console.log('4. 核对后关闭客户端，测试服务会自动结束。');
  launcher = spawn(exe, [`--proxy-server=http://127.0.0.1:${port}`, '--proxy-bypass-list=<-loopback>'], { stdio: 'ignore' });
  timer = setTimeout(() => { console.log('测试已达10分钟，将关闭本次测试客户端。需要时可重新运行入口。'); shutdown(); }, 600000);
  launcher.on('error', error => { console.error(error.message); process.exitCode = 1; shutdown(); });
  launcher.on('exit', code => { process.exitCode = code ?? 1; shutdown(); });
});
consoleInput.on('line', () => {
  proxy.enable();
  console.log('已放行本次客户端到固定平台的加密连接。现在回客户端点击“重试连接”。完成后关闭客户端。');
});
process.on('SIGINT', shutdown);
process.on('SIGHUP', shutdown);
consoleInput.on('SIGINT', shutdown);
process.on('exit', () => { if (launcher && launcher.exitCode === null) launcher.kill(); });
