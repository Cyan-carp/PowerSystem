import { afterEach, describe, expect, it, vi } from 'vitest'
afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })
describe('optional desktop adapter', () => {
  it('keeps browser and incompatible protocol available without native features', async () => {
    vi.stubGlobal('window', {})
    const desktop = await import('./desktop')
    await desktop.initDesktop()
    expect(Object.values(desktop.desktopFeatures.value).every(value => !value)).toBe(true)
    desktop.resetDesktopSession()
    await desktop.notifyDesktopAlarm(1, 'urgent')
  })
  it('invalidates queued old notifications and waits for the account reset', async () => {
    let complete!: () => void
    const notifyAlarm = vi.fn().mockResolvedValue({ submitted: true })
    vi.stubGlobal('window', { powerSystemDesktop: {
      getCapabilities: vi.fn().mockResolvedValue({ protocol: 'powersystem-desktop/v1', features: { notifications: true } }),
      resetNotificationSession: vi.fn(() => new Promise<void>(resolve => { complete = resolve })), notifyAlarm,
    } })
    const desktop = await import('./desktop'); await desktop.initDesktop()
    const stale = desktop.notifyDesktopAlarm(1, 'major')
    desktop.resetDesktopSession()
    await stale
    expect(notifyAlarm).not.toHaveBeenCalled()
    const fresh = desktop.notifyDesktopAlarm(2, 'minor')
    await Promise.resolve(); expect(notifyAlarm).not.toHaveBeenCalled()
    complete(); await fresh
    expect(notifyAlarm).toHaveBeenCalledOnce()
    expect(notifyAlarm.mock.calls[0][0].alarmId).toBe(2)
  })
})
