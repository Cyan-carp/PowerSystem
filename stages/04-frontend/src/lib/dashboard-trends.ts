type TrendPoint = [number, number]

/** 每个固定的30秒时间段保留最新实测值，避免滑动更新时间导致永远无法追加点。 */
export function appendEnergySample(points: TrendPoint[], sample: TrendPoint): TrendPoint[] {
  const last = points.at(-1)
  const sameBucket = last && Math.floor(sample[0] / 30_000) === Math.floor(last[0] / 30_000)
  return [...(sameBucket ? points.slice(0, -1) : points), sample].slice(-1500)
}
