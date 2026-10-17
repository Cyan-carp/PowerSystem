export type DailyPoint = [number, number | null]
export type Sample = [number, number]
const HOUR = 3_600_000
const BIN = 5 * 60_000
const MAX_GAP = 15_000

export function chinaDayStart(now: number): number {
  return Math.floor((now + 8 * HOUR) / (24 * HOUR)) * 24 * HOUR - 8 * HOUR
}

/** Aggregate raw device samples before display sampling; never integrate across outages. */
export function dailyGeneration(series: Sample[][], start: number, end: number): { power: DailyPoint[]; energy: DailyPoint[] } {
  const power = Array.from({ length: 288 }, () => ({ sum: 0, seen: false }))
  const energy = Array.from({ length: 24 }, () => ({ sum: 0, seen: false }))
  for (const samples of series) {
    const bins = new Map<number, { sum: number; count: number }>()
    for (const [time, value] of samples) {
      if (time < start || time > end || !Number.isFinite(value)) continue
      const index = Math.floor((time - start) / BIN)
      if (index < 0 || index >= power.length) continue
      const bin = bins.get(index) ?? { sum: 0, count: 0 }
      bin.sum += value; bin.count++
      bins.set(index, bin)
    }
    for (const [index, bin] of bins) {
      power[index].sum += bin.sum / bin.count
      power[index].seen = true
    }
    for (let i = 1; i < samples.length; i++) {
      const a = samples[i - 1], b = samples[i]
      const gap = b[0] - a[0]
      if (gap <= 0 || gap > MAX_GAP || !Number.isFinite(a[1]) || !Number.isFinite(b[1])) continue
      let left = Math.max(start, a[0])
      const right = Math.min(end, start + 24 * HOUR, b[0])
      while (left < right) {
        const hour = Math.floor((left - start) / HOUR)
        const stop = Math.min(right, start + (hour + 1) * HOUR)
        const pLeft = a[1] + (b[1] - a[1]) * (left - a[0]) / gap
        const pRight = a[1] + (b[1] - a[1]) * (stop - a[0]) / gap
        energy[hour].sum += (pLeft + pRight) / 2 * (stop - left) / HOUR
        energy[hour].seen = true
        left = stop
      }
    }
  }
  return {
    power: power.map((bin, i) => [(i + 0.5) / 12, bin.seen ? bin.sum : null]),
    energy: energy.map((bin, i) => [i + 0.5, bin.seen ? bin.sum : null]),
  }
}
