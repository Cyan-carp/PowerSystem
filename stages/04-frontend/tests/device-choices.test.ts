import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { useDeviceChoices } from '../src/lib/useDeviceChoices'

const devices = vi.hoisted(() => vi.fn())
vi.mock('../src/lib/api', () => ({ api: { devices } }))
const cleanups: (() => void)[] = []
function selector(id = 0) {
  const scope = effectScope(), selected = ref(id)
  const choices = scope.run(() => useDeviceChoices(selected))!
  cleanups.push(() => scope.stop())
  return { ...choices, selected }
}
const rows = (start: number, count: number) => Array.from({ length: count }, (_, i) => ({ id: start + i }))
beforeEach(() => devices.mockReset())
afterEach(() => cleanups.splice(0).forEach(cleanup => cleanup()))

it('loads all 143 devices in legal pages and preserves an existing selection', async () => {
  devices.mockResolvedValueOnce({ list: rows(1, 100), total: 143 }).mockResolvedValueOnce({ list: rows(101, 43), total: 143 })
  const choices = selector(143)
  await choices.load()
  expect(devices.mock.calls.map(call => call[0])).toEqual([{ page: 1, pageSize: 100, scope: 'all' }, { page: 2, pageSize: 100, scope: 'all' }])
  expect(choices.devices.value).toHaveLength(143)
  expect(choices.selected.value).toBe(143)
  await choices.load()
  expect(devices).toHaveBeenCalledTimes(2)
})
it('does not publish or cache a partial list and retries both pages successfully', async () => {
  devices.mockResolvedValueOnce({ list: rows(1, 100), total: 143 }).mockRejectedValueOnce(new Error('page two failed'))
  const choices = selector()
  await choices.load()
  expect(choices.devices.value).toEqual([])
  expect(choices.error.value).toBe('page two failed')
  devices.mockResolvedValueOnce({ list: rows(1, 100), total: 143 }).mockResolvedValueOnce({ list: rows(101, 43), total: 143 })
  await choices.load()
  expect(choices.error.value).toBe('')
  expect(choices.devices.value).toHaveLength(143)
  expect(choices.selected.value).toBe(1)
})
it('shares the same load between concurrent callers', async () => {
  let finish!: (value: unknown) => void
  devices.mockReturnValue(new Promise(resolve => { finish = resolve }))
  const choices = selector()
  const first = choices.load(), second = choices.load()
  expect(first).toBe(second)
  expect(devices).toHaveBeenCalledTimes(1)
  finish({ list: rows(1, 1), total: 1 })
  await Promise.all([first, second])
  expect(choices.devices.value).toHaveLength(1)
})
it('handles an empty ledger and refuses an incomplete pagination response', async () => {
  devices.mockResolvedValueOnce({ list: [], total: 0 })
  const empty = selector()
  await empty.load()
  expect(empty.devices.value).toEqual([])
  expect(empty.selected.value).toBe(0)
  expect(empty.error.value).toBe('')
  devices.mockResolvedValueOnce({ list: [], total: 143 })
  const incomplete = selector()
  await incomplete.load()
  expect(incomplete.error.value).toContain('不完整')
})
