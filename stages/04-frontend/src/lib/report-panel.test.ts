import { describe, expect, it } from 'vitest'
import { useReportPanel } from './report-panel'
const params = { start: '2026-10-18T00:00:00Z', end: '2026-10-19T00:00:00Z' }
function deferred<T>() {
  let resolve!: (value:T) => void, reject!: (error:Error) => void
  const promise = new Promise<T>((yes,no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
describe('independent report panels', () => {
  it('shows and enables export of the fast result while the other report is still pending or fails', async () => {
    const slow = deferred<string>()
    const availability = useReportPanel(() => slow.promise)
    const timeliness = useReportPanel(async () => 'confirmed')
    const waiting = availability.load(params)
    await timeliness.load(params)
    expect(timeliness.data.value).toBe('confirmed')
    expect(timeliness.loaded.value).toEqual(params)
    expect(availability.loading.value).toBe(true)
    expect(availability.loaded.value).toBeNull()
    slow.reject(new Error('telemetry unavailable')); await waiting
    expect(availability.error.value).toBe('telemetry unavailable')
    expect(timeliness.error.value).toBe('')
    expect(timeliness.data.value).toBe('confirmed')
  })
  it('discards superseded responses and cannot export a failed new selection', async () => {
    const first = deferred<string>(), second = deferred<string>(), third = deferred<string>()
    let calls = 0
    const panel = useReportPanel(() => (++calls === 1 ? first.promise : calls === 2 ? second.promise : third.promise))
    const a = panel.load(params), b = panel.load({...params, device_id:2})
    second.resolve('new'); await b
    first.resolve('old'); await a
    expect(panel.data.value).toBe('new')
    expect(panel.loaded.value?.device_id).toBe(2)
    const failed = panel.load({...params, device_id:3})
    expect(panel.data.value).toBeNull()
    expect(panel.loaded.value).toBeNull()
    third.reject(new Error('failed new selection')); await failed
    expect(panel.error.value).toBe('failed new selection')
    expect(panel.loaded.value).toBeNull()
    const failing = deferred<string>()
    const next = useReportPanel(() => failing.promise)
    const pending = next.load(params); next.cancel(); failing.resolve('late'); await pending
    expect(next.data.value).toBeNull()
    expect(next.loaded.value).toBeNull()
    expect(next.loading.value).toBe(false)
  })
})
