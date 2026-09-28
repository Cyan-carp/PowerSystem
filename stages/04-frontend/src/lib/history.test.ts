import { describe, expect, it } from 'vitest'
import { downsample } from './history'

describe('history chart decimation', () => {
  it('keeps a brief high temperature spike and both time boundaries', () => {
    const points: [number, number][] = Array.from({ length: 10000 }, (_, index) => [index, index === 4321 ? 85 : 30])
    const result = downsample(points, 1000)
    expect(result.length).toBeLessThanOrEqual(1000)
    expect(result[0]).toEqual([0, 30])
    expect(result.at(-1)).toEqual([9999, 30])
    expect(result).toContainEqual([4321, 85])
  })
})
