import { describe, expect, it, vi } from 'vitest'

vi.mock('./api', () => ({ api: { ticket: vi.fn() } }))

describe('realtime reconnection', () => {
  it('gets a fresh one-time ticket after a dropped WebSocket', async () => {
    vi.useFakeTimers()
    const values = new Map<string, string>()
    vi.stubGlobal('sessionStorage', { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) }, removeItem: (key: string) => { values.delete(key) } })
    vi.stubGlobal('location', { protocol: 'http:', host: '127.0.0.1:5173' })
    class FakeSocket {
      static instances: FakeSocket[] = []
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      constructor(public url: string) { FakeSocket.instances.push(this) }
      close(): void { this.onclose?.() }
    }
    vi.stubGlobal('WebSocket', FakeSocket)
    vi.resetModules()
    const { api } = await import('./api')
    vi.mocked(api.ticket).mockResolvedValueOnce({ ticket: 'first', expires_in: 60 }).mockResolvedValueOnce({ ticket: 'second', expires_in: 60 })
    const { token } = await import('./auth')
    token.value = 'token'
    const { realtime } = await import('./realtime')
    realtime.start()
    await Promise.resolve(); await Promise.resolve()
    expect(FakeSocket.instances[0].url).toContain('ticket=first')
    FakeSocket.instances[0].onopen?.()
    expect(realtime.connected.value).toBe(true)
    FakeSocket.instances[0].close()
    await vi.advanceTimersByTimeAsync(1000)
    expect(api.ticket).toHaveBeenCalledTimes(2)
    expect(FakeSocket.instances[1].url).toContain('ticket=second')
    realtime.stop()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })
})
