import { describe, expect, it } from 'vitest'
import { dateTime, normalizePoints, predictionState } from './format'

describe('visible date and time format', () => {
  it('uses dotted Beijing dates and keeps the time for timestamp values', () => {
    expect(dateTime('2026-09-27T17:06:00Z')).toBe('2026.9.28 01:06:00')
    expect(dateTime('2026-09-28')).toBe('2026.9.28')
    expect(dateTime('2026/9/28')).toBe('2026.9.28')
    expect(dateTime(null)).toBe('—')
    expect(dateTime('invalid')).toBe('—')
  })
})

describe('prediction display state', () => {
  it('keeps missing and stale predictions out of the current risk state', () => {
    expect(predictionState({ probability: null, stale: false })).toBe('missing')
    expect(predictionState({ probability: 0.9, stale: true })).toBe('stale')
    expect(predictionState({ probability: 0.9, stale: false })).toBe('fresh')
  })
})

describe('TDengine points', () => {
  it('normalizes millisecond timestamps and discards invalid points', () => {
    expect(normalizePoints([[2000, 2], ['1000', 1], ['bad', 9]])).toEqual([[1000, 1], [2000, 2]])
  })
})
