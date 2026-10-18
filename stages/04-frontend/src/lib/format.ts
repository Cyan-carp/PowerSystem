import type { AlarmLevel, AlarmStatus, Metric } from '../types'

export const metricLabels: Record<Metric, string> = { voltage: '电压', current: '电流', temperature: '温度', power: '功率' }
export const metricUnits: Record<Metric, string> = { voltage: 'V', current: 'A', temperature: '°C', power: 'kW' }
export const levelLabels: Record<AlarmLevel, string> = { urgent: '紧急', major: '重要', minor: '一般' }
export const statusLabels: Record<AlarmStatus, string> = { unhandled: '待确认', acked: '已确认', recovered: '已恢复' }

const beijingTime = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

export function dateTime(value: number | string | null | undefined): string {
  if (value == null || value === '') return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = Object.fromEntries(beijingTime.formatToParts(date).map(({ type, value }) => [type, value]))
  const day = `${parts.year}.${Number(parts.month)}.${Number(parts.day)}`
  if (typeof value === 'string' && /^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$/.test(value)) return day
  return `${day} ${parts.hour}:${parts.minute}:${parts.second}`
}

export function toMilliseconds(value: number | string): number {
  if (typeof value === 'number') return value
  if (/^\d+$/.test(value)) return Number(value)
  const parsed = Date.parse(value.includes(' ') ? value.replace(' ', 'T') : value)
  return Number.isFinite(parsed) ? parsed : NaN
}

/** Compact session activity time; full Beijing time stays available in the tooltip. */
export function sessionRecency(value: string | null | undefined, now = Date.now()): string {
  if (!value) return '—'
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return '—'
  const age = now - timestamp
  if (age >= -60_000 && age < 60_000) return '刚刚'
  if (age >= 60_000 && age < 3_600_000) return `${Math.floor(age / 60_000)}分钟前`
  if (age >= 3_600_000 && age < 86_400_000) return `${Math.floor(age / 3_600_000)}小时前`
  if (age >= 86_400_000 && age < 7 * 86_400_000) return `${Math.floor(age / 86_400_000)}天前`
  const parts = Object.fromEntries(beijingTime.formatToParts(new Date(timestamp)).map(({ type, value }) => [type, value]))
  const thisYear = beijingTime.formatToParts(new Date(now)).find(part => part.type === 'year')?.value
  return `${parts.year === thisYear ? '' : `${parts.year}-`}${parts.month}-${parts.day}`
}

export function normalizePoints(raw: [number | string, number][]): [number, number][] {
  return raw.map(([time, value]) => [toMilliseconds(time), Number(value)] as [number, number])
    .filter(([time, value]) => Number.isFinite(time) && Number.isFinite(value))
    .sort((a, b) => a[0] - b[0])
}

export function predictionState(item: { probability: number | null; stale: boolean }): 'fresh' | 'stale' | 'missing' {
  if (item.probability === null) return 'missing'
  return item.stale ? 'stale' : 'fresh'
}

export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : '操作失败，请重试' }
