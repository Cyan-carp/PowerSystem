/** 来源与证据的展示口径：链接白名单、类型中文名、副标题拼装。 */
import type { RAGSource } from './api'

/** 只放行绝对 http(s) 链接，且不允许带账号密码；其余一律不渲染成可点链接。 */
export function sourceLink(value: string | undefined | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch {
    return null
  }
}

export function sourceKindLabel(kind: RAGSource['kind']): string {
  return (
    {
      document: '知识库文档',
      web: '公网检索',
      business: '平台数据',
      infrastructure: '主机取证',
    }[kind] || '来源'
  )
}

/** 副标题：版本 / 章节 / 取证时间，缺什么就不写什么，不留空分隔符。 */
export function sourceSubtitle(item: RAGSource, stamp: (iso?: string | null) => string): string {
  const parts: string[] = []
  if (item.version) parts.push(`版本 ${item.version}`)
  if (item.page) parts.push(`第 ${item.page} 页`)
  if (item.collected_at) parts.push(stamp(item.collected_at))
  return parts.join(' · ')
}
