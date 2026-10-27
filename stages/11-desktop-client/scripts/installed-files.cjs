const path = require('node:path').win32;
function resolveInstallDirectory(record) {
  if (typeof record.InstallLocation === 'string' && path.isAbsolute(record.InstallLocation)) return record.InstallLocation;
  const icon = typeof record.DisplayIcon === 'string' ? record.DisplayIcon.replace(/,\s*-?\d+$/, '').replace(/^"|"$/g, '') : '';
  if (!path.isAbsolute(icon) || path.basename(icon).toLowerCase() !== 'powersystem desktop.exe') throw new Error('无法从安装登记确定程序目录，请反馈。');
  return path.dirname(icon);
}
module.exports = { resolveInstallDirectory };
