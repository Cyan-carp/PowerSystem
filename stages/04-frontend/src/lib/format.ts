import type { AlarmLevel, AlarmStatus, Metric } from '../types'

export const metricLabels: Record<Metric, string> = { voltage: '电压', current: '电流', temperature: '温度', power: '功率' }
export const metricUnits: Record<Metric, string> = { voltage: 'V', current: 'A', temperature: '°C', power: 'kW' }
export const levelLabels: Record<AlarmLevel, string> = { urgent: '紧急', major: '重要', minor: '一般' }
export const statusLabels: Record<AlarmStatus, string> = { unhandled: '待确认', acked: '已确认', recovered: '已恢复' }

export function dateTime(value: number | string | null | undefined): string {
  if (value == null || value === '') return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

export function toMilliseconds(value: number | string): number {
  if (typeof value === 'number') return value
  if (/^\d+$/.test(value)) return Number(value)
  const parsed = Date.parse(value.includes(' ') ? value.replace(' ', 'T') : value)
  return Number.isFinite(parsed) ? parsed : NaN
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
