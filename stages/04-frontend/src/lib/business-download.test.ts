import { afterEach, describe, expect, it, vi } from 'vitest'
import { alarmClosure, healthBasis } from './health'

afterEach(() => vi.unstubAllGlobals())

describe('business evidence and CSV downloads', () => {
  it('keeps administrative closure and missing evidence distinct from recovery', () => {
    expect(alarmClosure('rule_disabled')).toContain('管理关闭')
    expect(alarmClosure(null)).toContain('不能据此认定故障消除')
    expect(healthBasis('stale sample (>15s)')).toContain('采样已过期')
    expect(healthBasis('stale reception (>15s)')).toContain('不代表已证实硬件故障')
  })

  it('never saves a successful HTTP response with an error body as CSV', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:true,status:200,headers:new Headers({'Content-Type':'application/json'}),blob:vi.fn()}))
    const { downloadFile } = await import('./api')
    await expect(downloadFile('/fixture','fixture.csv')).rejects.toThrow('未返回 CSV')
  })

  it('does not create a downloadable file after an interrupted body read', async () => {
    const create=vi.fn()
    vi.stubGlobal('URL', {createObjectURL:create})
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:true,status:200,headers:new Headers({'Content-Type':'text/csv'}),blob:async()=>{throw new Error('body interrupted')}}))
    const { downloadFile } = await import('./api')
    await expect(downloadFile('/fixture','fixture.csv')).rejects.toThrow('body interrupted')
    expect(create).not.toHaveBeenCalled()
  })
})
