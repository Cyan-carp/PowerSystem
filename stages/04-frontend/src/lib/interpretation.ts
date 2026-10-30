/**
 * 告警解读的视图模型：把后端 `/agent/interpretations` 的 result 归一化成界面能直接渲染的几块。
 *
 * 为什么要这一层：
 * - 后端有两套协议（v1 / v2），v1 只有 conclusion/suggestions/limitations，v2 多 analysis_status/facts/hypotheses；
 *   字段缺失不能让界面崩，统一在这里兜。
 * - 解读是**异步任务**：pending / running 时 result 可能是上一轮的旧内容，不能当本轮答案显示。
 *   「生成中」和「没有解读」是两种状态，界面必须分开说。
 * - 模型给出的 conclusion.text 通常就是 findings 逐条拼起来的，直接两处都渲染会重复读一遍同样的话。
 */
import type { Interpretation, InterpretationEvidence, InterpretationFact, InterpretationResult } from './api'

/** pending=排队等模型；running=worker 正在算；completed=算完了看 result.status；degraded=没跑通 */
export const TASK_STATUS_CN: Record<string, string> = {
  pending: '排队中',
  running: '生成中',
  completed: '已完成',
  degraded: '未完成',
}

/**
 * 失败原因码 → 值班员能懂的一句话。
 * 这些码来自 Go 侧的 processInterpretation(V2)，不是猜的：配置、鉴权、额度、证据、熔断、provider 六类。
 */
export const INSIGHT_REASON_CN: Record<string, string> = {
  not_configured: '服务器未配置解读模型',
  auth_failed: '模型鉴权失败',
  quota_exhausted: '模型额度已用完',
  answer_truncated: '模型回答被截断',
  call_budget_exhausted: '本次调用的模型预算已用尽',
  retry_deadline_exceeded: '重试超时仍未完成',
  missing_event_evidence: '缺少事件证据，无法解读',
  evidence_unavailable: '证据采集失败',
  evidence_budget_exceeded: '证据过多，超出单次上限',
  circuit_open: '解读通道熔断中，稍后自动重试',
  model_state_unavailable: '解读通道状态读取失败',
  call_reservation_rejected: '本轮模型调用配额未抢到',
  rate_limited: '模型服务限流',
  model_timeout: '模型响应超时',
  model_unreachable: '模型服务不可达',
  model_non_json: '模型返回的不是合法 JSON',
  model_invalid_envelope: '模型返回未通过校验',
  model_empty_choices: '模型没有返回内容',
  model_empty_content: '模型回答为空',
  model_invalid_finish: '模型输出被异常中断',
  model_response_too_large: '模型回答过长',
  answer_validation_failed: '模型回答未通过校验',
  answer_schema_invalid: '模型回答格式不符',
  thinking_mode_mismatch: '模型思考模式与约定不符',
  unavailable: '解读通道暂不可用',
}

export function insightReasonText(code: string): string {
  if (!code) return ''
  return INSIGHT_REASON_CN[code] || `解读未完成（${code}）`
}

/** 证据来源（后端 tool 字段）中文名；认不出来的原样显示，不编 */
export const EVIDENCE_TOOL_CN: Record<string, string> = {
  alarm_record: '告警记录',
  device_telemetry: '设备遥测',
  latest_telemetry: '最新遥测',
  telemetry_window: '遥测窗口',
  monitor_event: '监控事件',
  device_health: '设备健康',
  prediction: '失效预测',
  rule_snapshot: '规则快照',
}

export type InsightState = 'missing' | 'pending' | 'ready' | 'degraded'

export interface InsightFactRow {
  label: string
  value: string
  unit: string
  /** 设备编码+名称这类长值独占一整行，挤在 1/2 列里会被省略号吃掉 */
  wide: boolean
}

export interface InsightEvidenceRow {
  id: string
  tool: string
  status: string
  at: string
}

export interface InsightView {
  fixed: boolean
  state: InsightState
  /** 状态行上的一句话，说明现在是什么阶段、为什么没有结论 */
  note: string
  /** 模型结论，已按 findings 拆条；degraded/missing 时为空 */
  conclusion: string[]
  facts: InsightFactRow[]
  /** 事实被截断时给出总条数，界面显示「另有 N 条」 */
  factTotal: number
  suggestions: string[]
  limitations: string[]
  evidence: InsightEvidenceRow[]
  model: string
  updatedAt: string
  reason: string
  protocol: number
  /** unable_to_determine：模型明确说证据不足，不算故障，也不该反复重试 */
  refused: boolean
}

const empty: InsightView = {
  fixed: false,
  state: 'missing',
  note: '后端还没有为这条告警生成解读',
  conclusion: [],
  facts: [],
  factTotal: 0,
  suggestions: [],
  limitations: [],
  evidence: [],
  model: '',
  updatedAt: '',
  reason: '',
  protocol: 0,
  refused: false,
}

/** 模型侧偶尔把 limitations 写成对象（v2 的 analysis 里就是 template+fact_refs），两种都收 */
function texts(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out: string[] = []
  for (const item of value) {
    if (typeof item === 'string') {
      if (item.trim()) out.push(item.trim())
    } else if (item && typeof item === 'object') {
      const text = (item as { text?: unknown }).text
      if (typeof text === 'string' && text.trim()) out.push(text.trim())
    }
  }
  return out
}

function factRows(facts: InterpretationFact[] | undefined): InsightFactRow[] {
  if (!Array.isArray(facts)) return []
  return facts
    .filter((item) => item && (item.display_value || item.value !== undefined))
    .map((item) => {
      const value = item.display_value || String(item.value ?? '')
      return {
        label: item.label || item.path || item.fact_id,
        value,
        unit: item.value_type === 'time' ? '' : item.unit || '',
        wide: item.role === 'device' || value.length > 20,
      }
    })
}

function evidenceRows(list: InterpretationEvidence[] | null | undefined): InsightEvidenceRow[] {
  if (!Array.isArray(list)) return []
  return list.map((item) => ({
    id: item.id,
    tool: (item.tool && EVIDENCE_TOOL_CN[item.tool]) || item.tool || item.source || '—',
    status: item.status || '—',
    at: item.data_time || item.collected_at || '',
  }))
}

/** conclusion.text 通常等于 findings 用换行拼接；相等时只留分条那份，避免同一段话读两遍 */
function conclusionLines(result: InterpretationResult): string[] {
  const findings = texts(result.findings)
  const text = result.conclusion?.text?.trim() || ''
  if (findings.length) {
    const joined = findings.join('\n')
    if (!text || text.replace(/\s+/g, '') === joined.replace(/\s+/g, '')) return findings
    return [text, ...findings]
  }
  return text ? [text] : []
}

/**
 * @param item 接口返回的解读记录；null/undefined 表示后端还没为这条告警建任务
 */
export function insightView(item: Interpretation | null | undefined): InsightView {
  if (!item) return { ...empty }

  const result = item.result || {}
  const base: InsightView = {
    ...empty,
    fixed: result.interpretation_mode === 'fixed',
    state: 'pending',
    reason: item.reason || result.reason || '',
    protocol: Number(result.protocol_version || 0),
    model: result.model || '',
    updatedAt: item.updated_at || '',
    facts: factRows(result.facts),
    factTotal: Array.isArray(result.facts) ? result.facts.length : 0,
    evidence: evidenceRows(result.evidence || item.evidence),
  }

  const status = String(result.status || '')
  const reasonText = insightReasonText(base.reason)
  const refused = status === 'unable_to_determine'

  if (item.task_status === 'pending' || item.task_status === 'running') {
    return {
      ...base,
      state: 'pending',
      note:
        item.task_status === 'running'
          ? '正在整理告警证据并生成解读'
          : '已进入解读队列，等待处理',
    }
  }

  if (status === 'answered') {
    const conclusion = conclusionLines(result)
    const limitations = texts(result.limitations)
    const suggestions = texts(result.suggestions)
    return {
      ...base,
      state: conclusion.length || suggestions.length ? 'ready' : 'degraded',
      note: base.fixed ? '固定规则解读 · 未调用模型API' : limitations.length ? '模型已给出结论，仍存在未解释的部分，见下方局限' : '模型基于事件证据给出结论',
      conclusion,
      suggestions,
      limitations,
      refused: false,
    }
  }

  if (refused) {
    return {
      ...base,
      state: 'degraded',
      refused: true,
      note: '模型判定现有证据不足，明确不给出结论',
      limitations: texts(result.limitations),
      suggestions: texts(result.suggestions),
    }
  }

  return {
    ...base,
    state: 'degraded',
    note: reasonText ? `解读未完成：${reasonText}` : '解读未完成，原因未记录',
    limitations: texts(result.limitations),
  }
}
