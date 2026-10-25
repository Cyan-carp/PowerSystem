import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({ token: { value: 'owner-token' }, clearSession: vi.fn() }))
vi.mock('./auth', () => auth)
import { downloadArtifact } from './agent-artifacts'
const id = '11111111-1111-4111-8111-111111111111'
const meta = { artifact_id: id, run_id: 'original-run', name: 'report.xlsx', status: 'ready', size: 4, sha256: '00'.repeat(32) }
describe('owner-bound artifact delivery', () => {
  beforeEach(() => { auth.token.value = 'owner-token' })
  afterEach(() => vi.unstubAllGlobals())
  it('rejects wrong run before asking for bytes', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ...meta, run_id: 'other-run' }) })
    vi.stubGlobal('fetch', fetch)
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('文件归属')
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer owner-token')
  })
  it('rejects an expired file returned by the authenticated download route', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => meta }).mockResolvedValueOnce({ ok: false, status: 410 }))
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('已过期或不可用')
  })
  it('does not save bytes if identity changes during the hash calculation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => meta }).mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }), blob: async () => new Blob(['test']) }))
    vi.stubGlobal('crypto', { subtle: { digest: async () => { auth.token.value = 'other-token'; return new Uint8Array(32).buffer } } })
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('登录身份已变化')
  })
  it('rejects incomplete downloads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => meta }).mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/csv' }), blob: async () => new Blob(['x']) }))
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('文件不完整')
  })
  it('rejects content with a mismatched digest before saving', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => meta }).mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/csv' }), blob: async () => new Blob(['test']) }))
    vi.stubGlobal('crypto', { subtle: { digest: async () => new Uint8Array(32).fill(1).buffer } })
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('文件校验失败')
  })
  it('accepts JSON evidence into the existing digest gate and rejects HTML', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => meta }).mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'application/json' }), blob: async () => new Blob(['test']) }))
    vi.stubGlobal('crypto', { subtle: { digest: async () => new Uint8Array(32).fill(1).buffer } })
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('文件校验失败')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => meta }).mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/html' }) }))
    await expect(downloadArtifact(id, 'original-run')).rejects.toThrow('服务器未返回文件')
  })
})
