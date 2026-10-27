import { app, BrowserWindow, dialog, ipcMain, Menu, screen, session, shell, Tray, nativeImage, Notification, powerMonitor, DownloadItem, IpcMainEvent, IpcMainInvokeEvent } from 'electron';
import { appendFileSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync, unlinkSync } from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { BUSINESS_ORIGIN, PROTOCOL, PARTITION, Caller, isBusinessCaller, isBusinessUrl, isExternalHttps, isFallbackCaller } from './policy';
import { Bounds, restoreState } from './window-state';
import { PreferencesStore, validatePreferencesPatch } from './preferences';
import { NotificationSession, validateAlarm } from './notification-session';
import { allowedDownload, safeFilename, completeSize } from './download-policy';

app.setName('PowerSystem Desktop');
// No command line switch or environment value selects a business origin.
app.enableSandbox();
app.commandLine.appendSwitch('disable-http-cache');
app.commandLine.appendSwitch('disable-extensions');
let win: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let hintShowing = false;
let preferences: PreferencesStore;
const notices = new NotificationSession();
const notifications = new Set<Notification>();
const downloads = new Set<DownloadItem>();
let startupComplete = false;
let loading = false;
let failureShown = false;
let pendingFocus = false;
let timeout: ReturnType<typeof setTimeout> | undefined;
const fallbackPath = app.isPackaged ? path.join(process.resourcesPath, 'fallback.html') : path.join(__dirname, '../src/fallback.html');
const fallbackUrl = pathToFileURL(fallbackPath).href;
const statePath = () => path.join(app.getPath('userData'), 'window-state.json');

function audit(code: string): void {
  // Only fixed event codes, never URLs, IPC payloads, auth values or page text.
  try {
    mkdirSync(app.getPath('userData'), { recursive: true });
    const log = path.join(app.getPath('userData'), 'security-events.jsonl');
    try { if (statSync(log).size > 262144) renameSync(log, log + '.1'); } catch { /* No previous log. */ }
    appendFileSync(log, JSON.stringify({ at: new Date().toISOString(), code }) + '\n');
  } catch { /* Logging must not prevent exit. */ }
}
function caller(event: IpcMainEvent | IpcMainInvokeEvent): Caller {
  return { owned: !!win && !win.isDestroyed() && event.sender === win.webContents,
    top: !!event.senderFrame && event.senderFrame === event.sender.mainFrame,
    frameUrl: event.senderFrame?.url ?? '', currentUrl: event.sender.getURL() };
}
function requireCaller(event: IpcMainEvent | IpcMainInvokeEvent, local = false): void {
  if (!(local ? isFallbackCaller(caller(event), fallbackUrl) : isBusinessCaller(caller(event)))) {
    audit('ipc-denied'); throw new Error('DESKTOP_CALLER_DENIED');
  }
}
function workAreas() {
  const primary = screen.getPrimaryDisplay();
  return [primary.workArea, ...screen.getAllDisplays().filter(d => d.id !== primary.id).map(d => d.workArea)];
}
function applyBounds(bounds: Bounds): void {
  if (!win || win.isDestroyed() || win.isMaximized()) return;
  // Windows can round framed DIP bounds at fractional display scaling. Correct against
  // the native result, with a bounded loop so repeated starts do not grow the window.
  let request = { ...bounds };
  for (let count = 0; count < 3; count++) {
    win.setBounds(request);
    const actual = win.getBounds();
    if (['x', 'y', 'width', 'height'].every(key => actual[key as keyof Bounds] === bounds[key as keyof Bounds])) break;
    request = { x: request.x + bounds.x - actual.x, y: request.y + bounds.y - actual.y,
      width: request.width + bounds.width - actual.width, height: request.height + bounds.height - actual.height };
  }
}
function saveWindow(): void {
  if (!win || win.isDestroyed() || win.isMinimized()) return;
  try {
    const state = { bounds: win.getNormalBounds(), maximized: win.isMaximized() };
    mkdirSync(app.getPath('userData'), { recursive: true });
    writeFileSync(statePath() + '.tmp', JSON.stringify(state)); renameSync(statePath() + '.tmp', statePath());
  } catch { audit('window-save-failed'); }
}
function focusWindow(): void {
  if (!win || win.isDestroyed()) { pendingFocus = true; return; }
  if (win.isMinimized()) win.restore();
  const restored = restoreState({ bounds: win.getNormalBounds(), maximized: win.isMaximized() }, workAreas());
  const current = win.getNormalBounds();
  if (!win.isMaximized() && JSON.stringify(current) !== JSON.stringify(restored.bounds)) applyBounds(restored.bounds);
  win.show(); win.focus();
}
function resetSession(): void {
  notices.reset();
  for (const notice of notifications) { notice.removeAllListeners(); notice.close(); }
  notifications.clear();
  for (const item of downloads) item.cancel();
  downloads.clear();
  audit('notification-session-reset');
}
function setPreferences(value: unknown) {
  const patch = validatePreferencesPatch(value);
  const previous = preferences.get();
  if (patch.launchAtLogin !== undefined) {
    try {
      app.setLoginItemSettings({ openAtLogin: patch.launchAtLogin, path: process.execPath, args: [] });
      if (app.getLoginItemSettings({ path: process.execPath, args: [] }).openAtLogin !== patch.launchAtLogin) throw new Error('launch');
    } catch { throw new Error('DESKTOP_STARTUP_SETTING_FAILED'); }
  }
  try {
    const result = preferences.update(patch);
    if (!result.notificationsEnabled) {
      for (const notice of notifications) { notice.removeAllListeners(); notice.close(); }
      notifications.clear();
    }
    return result;
  } catch {
    if (patch.launchAtLogin !== undefined) {
      try {
        app.setLoginItemSettings({ openAtLogin: previous.launchAtLogin, path: process.execPath, args: [] });
        if (app.getLoginItemSettings({ path: process.execPath, args: [] }).openAtLogin !== previous.launchAtLogin) audit('preference-rollback-failed');
      } catch { audit('preference-rollback-failed'); }
    }
    throw new Error('DESKTOP_PREFERENCE_SAVE_FAILED');
  }
}
async function openSettings(): Promise<void> {
  if (!win || win.isDestroyed()) return;
  focusWindow();
  const prefs = preferences.get();
  const { response } = await dialog.showMessageBox(win, { type: 'info', title: '桌面设置', message: '桌面偏好',
    detail: `系统通知：${prefs.notificationsEnabled ? '开' : '关'}\n开机启动：${prefs.launchAtLogin ? '开' : '关'}\n提醒受 Windows 设置、网络与休眠影响，不保证必达。显式退出后须重新登录。`,
    buttons: ['切换通知', '切换开机启动', '关闭'], cancelId: 2, defaultId: 2 });
  try {
    if (response === 0) setPreferences({ notificationsEnabled: !prefs.notificationsEnabled });
    if (response === 1) setPreferences({ launchAtLogin: !prefs.launchAtLogin });
  } catch { await dialog.showMessageBox(win, { type: 'error', message: '设置未能保存或系统未接受变更，请重试。' }); }
}
function createTray(): void {
  // Code-native small icon; no external image or executable resource.
  const pixels = Buffer.alloc(32 * 32 * 4);
  for (let y = 0; y < 32; y++) for (let x = 0; x < 32; x++) {
    const i = (y * 32 + x) * 4;
    if ((x - 16) ** 2 + (y - 16) ** 2 < 210) { pixels[i] = 110; pixels[i + 1] = 110; pixels[i + 2] = 20; pixels[i + 3] = 255; }
    if (x > 14 && x < 19 && y > 6 && y < 26) { pixels[i] = 255; pixels[i + 1] = 255; pixels[i + 2] = 255; pixels[i + 3] = 255; }
  }
  tray = new Tray(nativeImage.createFromBitmap(pixels, { width: 32, height: 32 }));
  tray.setToolTip('源网智联');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示窗口', click: focusWindow },
    { label: '隐藏窗口', click: () => win?.hide() },
    { label: '设置', click: () => { void openSettings(); } },
    { type: 'separator' }, { label: '退出', click: () => app.quit() }
  ]));
  tray.on('double-click', focusWindow);
}
function recoverConnection(): void {
  if (win && !win.isDestroyed() && isBusinessUrl(win.webContents.getURL())) {
    win.webContents.send('desktop:resume'); audit('connection-resume');
  }
}
async function showFailure(): Promise<void> {
  if (!win || win.isDestroyed() || startupComplete || failureShown) return;
  failureShown = true;
  clearTimeout(timeout); loading = false;
  audit('startup-load-failed');
  await win.loadFile(fallbackPath).catch(() => { audit('fallback-load-failed'); app.quit(); });
}
async function loadBusiness(): Promise<void> {
  if (!win || win.isDestroyed() || loading) return;
  loading = true; startupComplete = false; failureShown = false;
  timeout = setTimeout(() => { if (win && !win.isDestroyed()) { win.webContents.stop(); void showFailure(); } }, 30000);
  try {
    await win.loadURL(BUSINESS_ORIGIN + '/');
    if (win && isBusinessUrl(win.webContents.getURL())) { startupComplete = true; audit('startup-loaded'); }
  } catch { await showFailure(); }
  finally { clearTimeout(timeout); loading = false; }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('before-quit', () => { quitting = true; saveWindow(); resetSession(); tray?.destroy(); tray = null; });
  app.on('second-instance', () => { audit('second-instance-focused'); focusWindow(); });
  app.on('window-all-closed', () => app.quit());
  app.on('certificate-error', (event, _contents, _url, _error, _certificate, callback) => {
    event.preventDefault(); audit('certificate-denied'); callback(false);
  });
  app.whenReady().then(() => {
    app.setAppUserModelId('cn.cyancarp.powersystem.desktop'); Menu.setApplicationMenu(null);
    preferences = new PreferencesStore(app.getPath('userData'));
    // OS is authoritative; do not claim a stale file value as an effective setting.
    try { preferences.update({ launchAtLogin: app.getLoginItemSettings({ path: process.execPath, args: [] }).openAtLogin }); } catch { audit('preference-sync-failed'); }
    let saved: unknown;
    try { saved = JSON.parse(readFileSync(statePath(), 'utf8')); } catch { saved = undefined; }
    const state = restoreState(saved, workAreas());
    const memory = session.fromPartition(PARTITION, { cache: false });
    memory.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    memory.setPermissionCheckHandler(() => false);
    memory.setDevicePermissionHandler(() => false);
    memory.on('will-download', (event, item, contents) => {
      if (!win || contents !== win.webContents || !isBusinessUrl(contents.getURL()) || !allowedDownload(item.getInitiatorOrigin(), item.getURL(), item.getMimeType())) {
        event.preventDefault(); audit('download-denied');
        if (win && contents === win.webContents) void dialog.showMessageBox(win, { type: 'error', title: '文件保存', message: '保存失败', detail: '下载来源或文件类型未通过安全检查，请从平台原下载入口重试。' });
        return;
      }
      const epoch = notices.generation;
      const target = dialog.showSaveDialogSync(win, { title: '另存为', defaultPath: path.join(app.getPath('downloads'), safeFilename(item.getFilename())), properties: ['showOverwriteConfirmation'] });
      if (!target) {
        event.preventDefault(); audit('download-cancelled');
        void dialog.showMessageBox(win, { type: 'info', title: '文件保存', message: '已取消保存', detail: '未保存文件，不会自动重试。' });
        return;
      }
      // Stage only into a main-generated sibling. Never unlink a user-selected file
      // on cancel/failure, and replace it only after a complete validated download.
      const temporary = path.join(path.dirname(target), '.powersystem-' + randomUUID() + '.part');
      downloads.add(item);
      item.setSavePath(temporary);
      item.once('done', (_event, state) => {
        downloads.delete(item);
        let valid = false;
        if (state === 'completed') {
          try {
            valid = completeSize(item.getReceivedBytes(), item.getTotalBytes(), statSync(temporary).size);
            if (valid && notices.current(epoch)) renameSync(temporary, target);
            else valid = false;
          } catch { valid = false; }
        }
        const cancelled = state === 'cancelled';
        if (!valid) { try { unlinkSync(temporary); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') audit('download-partial-cleanup-failed'); } }
        audit(valid ? 'download-saved' : cancelled ? 'download-cancelled' : 'download-failed');
        if (!notices.current(epoch) || quitting || !win || win.isDestroyed()) return;
        void dialog.showMessageBox(win, { type: valid ? 'info' : cancelled ? 'info' : 'error', title: '文件保存',
          message: valid ? '保存成功' : cancelled ? '已取消保存' : '保存失败',
          detail: valid ? '文件已写入，接收字节数与落盘大小核对一致。' : cancelled ? '未保存文件，不会自动重试。' : '文件未能完整保存；请检查目录权限与可用空间后重新下载。' });
      });
    });
    win = new BrowserWindow({ ...state.bounds, minWidth: Math.min(800, state.bounds.width), minHeight: Math.min(600, state.bounds.height),
      title: '源网智联', show: false, autoHideMenuBar: true, frame: true,
      webPreferences: { preload: path.join(__dirname, 'preload.js'), session: memory,
        sandbox: true, contextIsolation: true, nodeIntegration: false, nodeIntegrationInSubFrames: false,
        webSecurity: true, allowRunningInsecureContent: false, webviewTag: false, devTools: !app.isPackaged,
        safeDialogs: true, navigateOnDragDrop: false, spellcheck: false }
    });
    if (state.maximized) win.maximize();
    win.once('ready-to-show', () => { win?.show(); if (!state.maximized) applyBounds(state.bounds); if (pendingFocus) { pendingFocus = false; focusWindow(); } });
    createTray();
    powerMonitor.on('resume', recoverConnection);
    powerMonitor.on('unlock-screen', recoverConnection);
    win.on('close', event => {
      saveWindow();
      if (quitting || !tray) return;
      event.preventDefault();
      if (!preferences.getTrayHintShown() && !hintShowing) {
        hintShowing = true;
        void dialog.showMessageBox(win!, { type: 'info', title: '已保留在托盘', message: '关闭窗口后仍在托盘运行', detail: '从右下角托盘可显示窗口、设置或显式退出。退出后须重新登录；提醒不保证必达。' }).finally(() => {
          try { preferences.markTrayHintShown(); } catch { audit('preference-save-failed'); }
          hintShowing = false; if (!quitting) win?.hide();
        });
      } else win?.hide();
      audit('window-hidden');
    });
    win.on('closed', () => { clearTimeout(timeout); win = null; });
    screen.on('display-removed', focusWindow);
    const wc = win.webContents;
    wc.setWindowOpenHandler(() => { audit('popup-denied'); return { action: 'deny' }; });
    wc.on('will-attach-webview', event => event.preventDefault());
    wc.on('will-frame-navigate', event => {
      if (!isBusinessUrl(event.url)) { event.preventDefault(); audit('navigation-denied'); }
    });
    wc.on('will-redirect', (event, url) => { if (!isBusinessUrl(url)) { event.preventDefault(); audit('redirect-denied'); } });
    // Prevent generic file: fetch/navigation even from a compromised remote renderer.
    memory.webRequest.onBeforeRequest({ urls: ['file://*/*'] }, (details, callback) => callback({ cancel: details.url !== fallbackUrl || details.resourceType !== 'mainFrame' }));
    ipcMain.handle('desktop:capabilities', (event, ...args) => {
      requireCaller(event); if (args.length) throw new Error('DESKTOP_ARGUMENTS_DENIED');
      return { protocol: PROTOCOL, appVersion: app.getVersion(), features: { notifications: Notification.isSupported(), preferences: true, saveAs: true, tray: !!tray } };
    });
    ipcMain.handle('desktop:preferences:get', (event, ...args) => { requireCaller(event); if (args.length) throw new Error('DESKTOP_ARGUMENTS_DENIED'); return preferences.get(); });
    ipcMain.handle('desktop:preferences:set', (event, ...args) => { requireCaller(event); if (args.length !== 1) throw new Error('DESKTOP_ARGUMENTS_DENIED'); return setPreferences(args[0]); });
    ipcMain.handle('desktop:session:reset', (event, ...args) => { requireCaller(event); if (args.length) throw new Error('DESKTOP_ARGUMENTS_DENIED'); resetSession(); });
    ipcMain.handle('desktop:alarm', (event, ...args) => {
      requireCaller(event); if (args.length !== 1) throw new Error('DESKTOP_ARGUMENTS_DENIED');
      const alarm = validateAlarm(args[0]);
      if (!preferences.get().notificationsEnabled) return { submitted: false, reason: 'muted' };
      if (win!.isVisible() && win!.isFocused()) return { submitted: false, reason: 'foreground' };
      if (!Notification.isSupported()) return { submitted: false, reason: 'unsupported' };
      if (!notices.claim(alarm.alarmId)) return { submitted: false, reason: 'duplicate' };
      const epoch = notices.generation;
      try {
        // Fixed generic content avoids placing device data or page text on the lock screen.
        const notice = new Notification({ title: '源网智联 · 新告警', body: '平台有一条新告警，请打开告警详情核查。', silent: false });
        notifications.add(notice);
        notice.on('click', () => {
          if (!notices.current(epoch) || quitting || !win || !isBusinessUrl(win.webContents.getURL())) return;
          focusWindow(); win.webContents.send('desktop:alarm:open', alarm.alarmId); audit('notification-clicked');
        });
        notice.once('close', () => notifications.delete(notice));
        notice.once('failed', () => { notifications.delete(notice); audit('notification-failed'); });
        notice.show(); audit('notification-submitted'); return { submitted: true };
      } catch { audit('notification-failed'); return { submitted: false, reason: 'failed' }; }
    });
    ipcMain.handle('startup:retry', (event, ...args) => { requireCaller(event, true); if (args.length) throw new Error('DESKTOP_ARGUMENTS_DENIED'); void loadBusiness(); });
    ipcMain.handle('startup:exit', (event, ...args) => { requireCaller(event, true); if (args.length) throw new Error('DESKTOP_ARGUMENTS_DENIED'); app.quit(); });
    ipcMain.on('desktop:external-click', (event, url, ...args) => {
      try { requireCaller(event); if (args.length || !isExternalHttps(url)) throw new Error('DESKTOP_URL_DENIED');
        void shell.openExternal(url).catch(() => audit('external-open-failed'));
      } catch { audit('external-click-denied'); }
    });
    void loadBusiness();
  }).catch(() => { audit('startup-fatal'); app.quit(); });
}
