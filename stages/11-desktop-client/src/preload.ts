// Sandboxed preload must remain self-contained: only Electron's restricted require.
import { contextBridge, ipcRenderer } from 'electron';

if (process.isMainFrame && location.origin === 'https://www.cyancarp.cn') {
  contextBridge.exposeInMainWorld('powerSystemDesktop', Object.freeze({
    getCapabilities: (...args: unknown[]) => ipcRenderer.invoke('desktop:capabilities', ...args),
    notifyAlarm: (...args: unknown[]) => ipcRenderer.invoke('desktop:alarm', ...args),
    getPreferences: (...args: unknown[]) => ipcRenderer.invoke('desktop:preferences:get', ...args),
    setPreferences: (...args: unknown[]) => ipcRenderer.invoke('desktop:preferences:set', ...args),
    resetNotificationSession: (...args: unknown[]) => ipcRenderer.invoke('desktop:session:reset', ...args)
  }));
  ipcRenderer.on('desktop:alarm:open', (_event, id: unknown) => {
    if (Number.isSafeInteger(id) && (id as number) > 0) window.dispatchEvent(new CustomEvent('powersystem-desktop-alarm', { detail: id }));
  });
  ipcRenderer.on('desktop:resume', () => window.dispatchEvent(new Event('powersystem-desktop-resume')));
  // Only a trusted DOM click can request an external browser. No generic URL API is exposed.
  document.addEventListener('click', event => {
    if (!event.isTrusted || event.button !== 0 || event.defaultPrevented) return;
    const anchor = event.composedPath().find(node => node instanceof HTMLAnchorElement) as HTMLAnchorElement | undefined;
    if (!anchor) return;
    try {
      const url = new URL(anchor.href);
      if (url.protocol === 'https:' && url.origin !== location.origin && !url.username && !url.password) {
        event.preventDefault();
        ipcRenderer.send('desktop:external-click', url.href);
      }
    } catch { /* Invalid links are left to the navigation deny policy. */ }
  }, true);
} else if (process.isMainFrame && location.protocol === 'file:' && location.pathname.endsWith('/fallback.html')) {
  // Main process independently checks the exact packaged file and invoking frame.
  contextBridge.exposeInMainWorld('powerSystemStartup', Object.freeze({
    retry: () => ipcRenderer.invoke('startup:retry'),
    exit: () => ipcRenderer.invoke('startup:exit')
  }));
  window.addEventListener('DOMContentLoaded', () => {
    document.getElementById('retry')?.addEventListener('click', () => { void ipcRenderer.invoke('startup:retry'); });
    document.getElementById('exit')?.addEventListener('click', () => { void ipcRenderer.invoke('startup:exit'); });
  });
}
