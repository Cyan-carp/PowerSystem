import { describe, expect, it } from 'vitest'
import { appendEnergySample } from './dashboard-trends'

describe('dashboard energy sampling', () => {
  it('keeps accumulating points during continuous ten-second refreshes', () => {
    const start = Date.UTC(2026, 9, 15, 6)
    let points: [number, number][] = []
    for (let index = 0; index <= 12; index++) {
      points = appendEnergySample(points, [start + index * 10_000, 100 + index / 10])
    }
    expect(points).toEqual([
      [start + 20_000, 100.2],
      [start + 50_000, 100.5],
      [start + 80_000, 100.8],
      [start + 110_000, 101.1],
      [start + 120_000, 101.2],
    ])
  })

  it('preserves observed corrections and gaps without inventing intermediate values', () => {
    const start = Date.UTC(2026, 9, 15, 6)
    const first: [number, number][] = [[start, 50]]
    const corrected = appendEnergySample(first, [start + 10_000, 49.9])
    expect(corrected).toEqual([[start + 10_000, 49.9]])
    expect(appendEnergySample(corrected, [start + 120_000, 51])).toEqual([
      [start + 10_000, 49.9], [start + 120_000, 51],
    ])
  })
})
