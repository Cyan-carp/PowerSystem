import { describe, expect, it, vi } from 'vitest'

describe('login session', () => {
  it('survives a module reload in the same browser tab and clears on logout', async () => {
    const values = new Map<string, string>()
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
    })
    vi.resetModules()
    const first = await import('./auth')
    first.saveSession({ token: 'test-token', token_type: 'Bearer', expires_in: 28800, user: { id: 1, username: 'operator', real_name: '值班员', role: 'operator' } })
    vi.resetModules()
    const reloaded = await import('./auth')
    expect(reloaded.token.value).toBe('test-token')
    expect(reloaded.currentUser.value?.username).toBe('operator')
    reloaded.clearSession()
    expect(values.size).toBe(0)
    vi.unstubAllGlobals()
  })
})
