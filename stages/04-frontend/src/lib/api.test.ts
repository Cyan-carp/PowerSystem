import { describe, expect, it, vi } from 'vitest'

describe('API authorization', () => {
  it('clears the session and returns to login after a protected request receives 401', async () => {
    const values = new Map<string, string>()
    const replace = vi.fn()
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
    })
    vi.stubGlobal('window', { location: { replace } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({ code: 40101, message: 'token expired' }) }))
    vi.resetModules()
    const auth = await import('./auth')
    auth.saveSession({ token: 'expired', token_type: 'Bearer', expires_in: 1, user: { id: 1, username: 'u', real_name: '', role: 'operator' } })
    const { request } = await import('./api')
    await expect(request('/api/v1/devices')).rejects.toMatchObject({ status: 401, code: 40101 })
    expect(auth.token.value).toBe('')
    expect(values.has('powersystem_token')).toBe(false)
    expect(replace).toHaveBeenCalledWith('/login')
    vi.unstubAllGlobals()
  })
  it('keeps Retry-After on a non-JSON 429 response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 429, headers: new Headers({ 'Retry-After': '8' }), json: async () => { throw new SyntaxError('plain-text rate limit') } }))
    const { request } = await import('./api')
    await expect(request('/api/v1/agent/tasks/fixture')).rejects.toMatchObject({ status: 429, retryAfter: 8 })
    vi.unstubAllGlobals()
  })
  it('archives an alarm through the soft-delete endpoint', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers(),
      json: async () => ({ code: 0, message: 'success', data: { id: 42, archived: true } }),
    })
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    await expect(api.archiveAlarm(42)).resolves.toMatchObject({ id: 42, archived: true })
    expect(fetch).toHaveBeenCalledWith('/api/v1/alarms/42/archive', expect.objectContaining({ method: 'POST' }))
    vi.unstubAllGlobals()
  })
})
