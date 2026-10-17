import type { RAGEvidence } from '../types'
import { dateTime } from './format'

export function evidenceLabel(state: RAGEvidence['state']): string {
  return { not_queried: '本轮未查询平台数据', queried: '平台查询成功', partial_failed: '平台查询部分失败', failed: '平台查询失败' }[state]
}
export function dataTime(value?: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return '缺失或无法解析'
  return `${dateTime(value)}（北京时间）`
}
export function freshnessLabel(value: string): string {
  return { fresh: '数据在时效范围内', stale: '数据已过期', future: '时间异常：采样时间在未来', unknown: '新鲜度无法判断' }[value] || '新鲜度无法判断'
}
