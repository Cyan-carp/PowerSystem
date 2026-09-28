import { describe, expect, it } from 'vitest'
import { normalizePoints, predictionState } from './format'

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
