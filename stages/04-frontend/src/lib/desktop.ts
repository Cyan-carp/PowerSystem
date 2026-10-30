import { ref } from 'vue'

interface Preferences { notificationsEnabled: boolean; launchAtLogin: boolean }
interface DesktopBridge {
  getCapabilities(): Promise<{ protocol: string; features: { notifications?: boolean; preferences?: boolean; saveAs?: boolean; tray?: boolean } }>
  notifyAlarm?(alarm: { alarmId: number; level: 'urgent' | 'major' | 'minor'; title: string; body: string }): Promise<{ submitted: boolean; reason?: string }>
  resetNotificationSession?(): Promise<void>
  getPreferences?(): Promise<Preferences>
  setPreferences?(value: Partial<Preferences>): Promise<Preferences>
}
declare global { interface Window { powerSystemDesktop?: DesktopBridge } }
export const desktopFeatures = ref({ notifications: false, preferences: false, saveAs: false, tray: false })
let epoch = 0
let pendingReset: Promise<void> = Promise.resolve()
const sessionListeners = new Set<() => void>()
function bridge(): DesktopBridge | undefined { return typeof window === 'undefined' ? undefined : window.powerSystemDesktop }
export async function initDesktop(): Promise<void> {
  try {
    const caps = await bridge()?.getCapabilities()
    if (caps?.protocol === 'powersystem-desktop/v1') desktopFeatures.value = {
      notifications: caps.features.notifications === true, preferences: caps.features.preferences === true,
      saveAs: caps.features.saveAs === true, tray: caps.features.tray === true,
    }
  } catch { /* optional bridge never blocks web login */ }
}
export function sessionEpoch(): number { return epoch }
export function onSessionReset(listener: () => void): () => void { sessionListeners.add(listener); return () => sessionListeners.delete(listener) }
export function resetDesktopSession(): void {
  epoch++
  for (const listener of sessionListeners) listener()
  // Invoke immediately, before new account content or notifications can be queued.
  try { pendingReset = bridge()?.resetNotificationSession?.() ?? Promise.resolve() }
  catch { pendingReset = Promise.reject(new Error('桌面通知会话未能重置')) }
  void pendingReset.catch(() => {})
}
export async function notifyDesktopAlarm(alarmId: number, level: 'urgent' | 'major' | 'minor'): Promise<void> {
  const mine = epoch
  try {
    await pendingReset
    if (mine !== epoch || !desktopFeatures.value.notifications) return
    await bridge()?.notifyAlarm?.({ alarmId, level, title: '平台新告警', body: '请打开告警详情核查。' })
  } catch { /* Windows notifications are best effort; page alarm remains available. */ }
}
export async function getDesktopPreferences(): Promise<Preferences | undefined> { return bridge()?.getPreferences?.() }
export async function setDesktopPreferences(value: Partial<Preferences>): Promise<Preferences | undefined> { return bridge()?.setPreferences?.(value) }
