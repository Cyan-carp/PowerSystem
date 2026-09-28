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
})
