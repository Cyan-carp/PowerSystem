import type { Scope, Station } from '../types'

export const defaultScope: Scope = 'business'

export const scopeLabels: Record<Scope, string> = {
  business: '业务范围',
  all: '全平台',
  baseline: '基线演示',
}

export const scopeOptions: { value: Scope; label: string }[] = [
  { value: 'business', label: scopeLabels.business },
  { value: 'all', label: scopeLabels.all },
  { value: 'baseline', label: scopeLabels.baseline },
]

/**
 * 只有基线范围允许逐台加载全天曲线。
 * 业务/全平台范围必须使用服务端聚合，不能对每台设备调用 history。
 */
export function allowsDailyCurve(scope: Scope): boolean {
  return scope === 'baseline'
}

/** 切换范围后保留仍属于新范围的场站，否则清空筛选。 */
export function reconcileStation(stationCode: string, scope: Scope, stations: Station[]): string {
  if (!stationCode) return ''
  const found = stations.some((station) => station.station_code === stationCode)
  if (!found) return ''
  if (scope === 'all') return stationCode
  return stations.some((station) => station.station_code === stationCode && station.scope === scope) ? stationCode : ''
}
