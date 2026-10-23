import { describe, expect, it } from 'vitest'
import { dateTime, durationText, normalizePoints, predictionState } from './format'

describe('visible date and time format', () => {
  it('uses dotted Beijing dates and keeps the time for timestamp values', () => {
    expect(dateTime('2026-09-27T17:06:00Z')).toBe('2026.9.28 01:06:00')
    expect(dateTime('2026-09-28')).toBe('2026.9.28')
    expect(dateTime('2026/9/28')).toBe('2026.9.28')
    expect(dateTime(null)).toBe('—')
    expect(dateTime('invalid')).toBe('—')
  })
})

describe('report durations', () => {
  it('shows hours/minutes/seconds and drops zero leading units', () => {
    expect(durationText(3_723_000)).toBe('1时2分3秒')
    expect(durationText(3_600_000)).toBe('1时0秒')
    expect(durationText(300_000)).toBe('5分0秒')
    expect(durationText(45_000)).toBe('45秒')
    expect(durationText(999)).toBe('<1秒')
    expect(durationText(0)).toBe('0秒')
  })

  it('keeps unknown durations out of numeric display', () => {
    expect(durationText(null)).toBe('—')
    expect(durationText(undefined)).toBe('—')
    expect(durationText(-1)).toBe('—')
    expect(durationText(Number.NaN)).toBe('—')
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
