import type { Interpretation } from '../types'

export function attentionOrder(items: Interpretation[]): Interpretation[] {
  const rank = { urgent: 0, major: 1, minor: 2 }
  return [...items].sort((a, b) => rank[a.level] - rank[b.level] || Date.parse(b.occurred_at) - Date.parse(a.occurred_at) || b.id - a.id)
}

export function shouldAutoOpen(item: Interpretation, seen: Set<number>): boolean {
  return item.active && !item.read && !seen.has(item.id)
}

export const reasonLabels: Record<string, string> = {
  not_configured: '尚未接入可用模型', auth_failed: '模型认证失败', quota_exhausted: '供应商报告额度不足',
  rate_limited: '模型请求受到限流', unavailable: '模型服务暂不可用', answer_validation_failed: '回答未通过证据校验',
}
