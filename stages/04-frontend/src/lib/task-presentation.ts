import type { OpsTask, RAGEvidence } from '../types'
import { dateTime } from './format'

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

/** Presentation only. Code, references, identifiers and timestamps retain their original precision. */
export function readableTaskText(value: unknown): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value) ?? ''
  const protectedParts = /(```[\s\S]*?```|`[^`\n]*`|!?\[[^\]]*\]\([^\n)]*\)|https?:\/\/[^\s，。；）]+|\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:[T ][\d:.+Z-]+)?|[A-Za-z_][\w./:@=+\-]*)/g
  const formatted = text.split(protectedParts).map((part, index) => index % 2 ? part : part.replace(/(?<![\w./:#=])[-+]?\d+\.\d+(?![\w./])/g, number => Number(number).toFixed(2))).join('')
  // Existing Markdown owns its layout. Only unstructured prose receives sentence breaks.
  if (/`|\[[^\]]*\]\(|\n\s*\n|^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)、]\s|>|\|)/m.test(formatted)) return formatted
  let depth = 0, line = '', paragraphs: string[] = []
  for (const char of formatted) {
    if ('（([{【'.includes(char)) depth++
    if ('）)]}】'.includes(char)) depth = Math.max(0, depth - 1)
    line += char
    if (depth === 0 && (/[。！？；\n]/.test(char) || (char === '，' && line.length >= 110))) {
      paragraphs.push(line.trim()); line = ''
    }
  }
  if (line.trim()) paragraphs.push(line.trim())
  return paragraphs.filter(Boolean).join('\n\n')
}

export function hasBusinessEvidence(evidence?: RAGEvidence): boolean {
  return !!evidence && (evidence.state !== 'not_queried' || evidence.cards.length > 0 || evidence.success_count > 0 || evidence.failed_count > 0)
}

export interface TaskFactGroup {
  step: number; tool: string; status: string; collected: string; limited: boolean
  fields: { label: string; value: string }[]; digest: string
}

/** Project fixed, already-public result fields; never extract program facts from model prose. */
export function taskFactGroups(task: OpsTask): TaskFactGroup[] {
  return (task.steps ?? []).map(step => {
    const result = record(step.result)
    const fields: TaskFactGroup['fields'] = []
    const add = (key: string, label: string) => {
      const value = result[key]
      if (typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number') {
        fields.push({ label, value: typeof value === 'boolean' ? (value ? '是' : '否') : typeof value === 'number' && !Number.isInteger(value) ? value.toFixed(2) : String(value) })
      }
    }
    if (step.status === 'succeeded' && step.tool === 'docker.inspect_container') {
      add('state', '容器状态'); add('health', '健康检查'); add('running', '进程运行'); add('restart_count', '重启次数')
      for (const [key, label] of [['started_at', '启动时间'], ['finished_at', '上次结束时间']] as const) {
        if (typeof result[key] === 'string') fields.push({ label, value: dateTime(result[key]) })
      }
    }
    if (step.status === 'succeeded' && step.tool === 'assets.list' && Array.isArray(result.list)) {
      for (const item of result.list) {
        const asset = record(item)
        if (typeof asset.asset_id === 'string') fields.push({ label: typeof asset.display_name === 'string' ? asset.display_name : '返回资产', value: asset.asset_id })
      }
    }
    return { step: step.step_no, tool: step.tool, status: step.status,
      collected: typeof result.collected_at === 'string' ? dateTime(result.collected_at) : '',
      limited: result.complete === false || result.truncated === true,
      fields, digest: step.tool === 'docker.inspect_container' && step.status === 'succeeded' && typeof result.image_digest === 'string' ? result.image_digest : '' }
  })
}
