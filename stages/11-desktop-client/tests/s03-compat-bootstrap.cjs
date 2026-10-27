// Browser-only compatibility fixture; never packaged or connected to production.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const run = require('../scripts/test-run-directory.cjs').testRunDirectory();
if (!['browser', 's01', 'incompatible'].includes(process.env.S03_COMPAT_MODE)) throw new Error('Unknown compatibility mode');
app.setPath('userData', path.join(run, 'compat-' + process.env.S03_COMPAT_MODE));
app.enableSandbox();
app.whenReady().then(() => {
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  void win.loadURL('about:blank');
});
app.on('window-all-closed', () => app.quit());
