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

  it('passes scope and station filters through device and alarm queries', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers(),
      json: async () => ({ code: 0, message: 'success', data: { list: [], page: 1, page_size: 20, total: 0 } }),
    })
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    await api.devices(2, 50, 'PV-01-01区', { scope: 'business', stationCode: 'PV-01' })
    const deviceUrl = new URL(fetch.mock.calls[0][0] as string, 'http://localhost')
    expect(deviceUrl.pathname).toBe('/api/v1/devices')
    expect(deviceUrl.searchParams.get('page')).toBe('2')
    expect(deviceUrl.searchParams.get('page_size')).toBe('50')
    expect(deviceUrl.searchParams.get('group_name')).toBe('PV-01-01区')
    expect(deviceUrl.searchParams.get('scope')).toBe('business')
    expect(deviceUrl.searchParams.get('station_code')).toBe('PV-01')

    await api.alarms(1, 20, 'unhandled', 'major', { scope: 'baseline', stationCode: 'ST-01' })
    const alarmUrl = new URL(fetch.mock.calls[1][0] as string, 'http://localhost')
    expect(alarmUrl.pathname).toBe('/api/v1/alarms')
    expect(alarmUrl.searchParams.get('status')).toBe('unhandled')
    expect(alarmUrl.searchParams.get('level')).toBe('major')
    expect(alarmUrl.searchParams.get('scope')).toBe('baseline')
    expect(alarmUrl.searchParams.get('station_code')).toBe('ST-01')
    vi.unstubAllGlobals()
  })

  it('keeps legacy calls scope-free and queries stations/overview with scope', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers(),
      json: async () => ({ code: 0, message: 'success', data: { list: [], total: 0, stations: [] } }),
    })
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    await api.devices()
    const legacy = new URL(fetch.mock.calls[0][0] as string, 'http://localhost')
    expect(legacy.searchParams.get('scope')).toBeNull()
    expect(legacy.searchParams.get('station_code')).toBeNull()

    await api.stations('business')
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/stations?scope=business')

    await api.overview('all', 'PV-02')
    const overviewUrl = new URL(fetch.mock.calls[2][0] as string, 'http://localhost')
    expect(overviewUrl.pathname).toBe('/api/v1/dashboard/overview')
    expect(overviewUrl.searchParams.get('scope')).toBe('all')
    expect(overviewUrl.searchParams.get('station_code')).toBe('PV-02')
    vi.unstubAllGlobals()
  })
})
