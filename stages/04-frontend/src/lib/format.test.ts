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
  it('formats durations as zero-padded HH:MM:SS', () => {
    expect(durationText(3_723_000)).toBe('01:02:03')
    expect(durationText(3_600_000)).toBe('01:00:00')
    expect(durationText(300_000)).toBe('00:05:00')
    expect(durationText(45_000)).toBe('00:00:45')
    expect(durationText(999)).toBe('<00:00:01')
    expect(durationText(0)).toBe('00:00:00')
    expect(durationText(494_228_000)).toBe('137:17:08')
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
