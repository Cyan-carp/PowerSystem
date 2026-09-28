import { api } from './api'
import { normalizePoints } from './format'
import type { Metric } from '../types'

const FOUR_HOURS = 4 * 60 * 60 * 1000
const MAX_SPAN = 24 * 60 * 60 * 1000

export function downsample(points: [number, number][], limit = 1800): [number, number][] {
  if (points.length <= limit) return points
  const result: [number, number][] = [points[0]]
  const bucketCount = Math.floor((limit - 2) / 2)
  const inner = points.slice(1, -1)
  for (let bucket = 0; bucket < bucketCount; bucket++) {
    const start = Math.floor(bucket * inner.length / bucketCount)
    const end = Math.floor((bucket + 1) * inner.length / bucketCount)
    const sample = inner.slice(start, end)
    if (!sample.length) continue
    let min = sample[0], max = sample[0]
    for (const point of sample) {
      if (point[1] < min[1]) min = point
      if (point[1] > max[1]) max = point
    }
    if (min[0] === max[0]) result.push(min)
    else result.push(...(min[0] < max[0] ? [min, max] : [max, min]))
  }
  result.push(points[points.length - 1])
  return result
}

export async function loadHistory(id: number, metric: Metric, start: Date, end: Date): Promise<[number, number][]> {
  const span = end.getTime() - start.getTime()
  if (span <= 0 || span > MAX_SPAN) throw new Error('时间范围必须大于 0 且不超过 24 小时')
  const ranges: [Date, Date][] = []
  for (let cursor = start.getTime(); cursor < end.getTime(); cursor += FOUR_HOURS) {
    ranges.push([new Date(cursor), new Date(Math.min(cursor + FOUR_HOURS, end.getTime()))])
  }
  const chunks = await Promise.all(ranges.map(([from, to]) => api.history(id, metric, from, to)))
  const merged = normalizePoints(chunks.flatMap((item) => item.points))
  const unique = merged.filter((point, index) => index === 0 || point[0] !== merged[index - 1][0])
  return downsample(unique)
}
