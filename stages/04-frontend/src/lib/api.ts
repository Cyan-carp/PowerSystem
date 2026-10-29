/**
 * 接口层：逐条对齐 stages/02-backend 的 Gin 路由（internal/api/server.go 第 90–186 行）。
 *
 * 约定：
 * - 响应统一是 { code, message, data }，code !== 0 即失败，401 表示登录失效。
 * - 分页统一返回 { list, page, page_size, total }，page_size 上限 100。
 * - 时间一律 RFC3339（UTC），展示时再换北京时间。
 * - 本文件里出现的路径必须能在真实后端找到同名路由；假后端只负责实现同一份契约。
 */

import type { OpsStatus, OpsTask } from './ops-types'
import { authToken, notifyUnauthorized } from './auth'
import type { LoginResult } from './auth'

export interface Envelope<T> {
  code: number
  message: string
  data: T
}


export class ApiError extends Error {
  constructor(
    public status: number,
    public code: number,
    message: string,
  ) {
    super(message)
  }
}

/**
 * 鉴权头由 auth 层统一给：没有 token 就不带 Authorization（后端会回 401，
 * 由 notifyUnauthorized 送回登录页），这里不再用假 token 兜底——那样只会把
 * “没登录”伪装成“接口没数据”，排查时看不出来。
 */
function authHeaders(init: RequestInit = {}): Headers {
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (authToken.value) headers.set('Authorization', `Bearer ${authToken.value}`)
  return headers
}

/** 所有通道共用：401 一律先清会话再抛错，调用方不必各自判状态码 */
function catchAuth(status: number): void {
  if (status === 401) notifyUnauthorized()
}

/**
 * 告警写操作的状态码有确定含义，不能把后端英文原样吐给值班员：
 * 409 = 已被别人确认/已恢复（并发重复处置），404 = 已归档或不存在，503 = 维护窗口内写入被暂停。
 */
export function alarmWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return '该告警已被确认或已恢复，请刷新后查看'
    if (error.status === 404) return '告警不存在或已归档'
    if (error.status === 503) return '服务正在维护或启动恢复中，写操作暂不可用'
    if (error.status === 401) return '登录已失效，请重新登录'
    if (error.status === 0) return '无法连接后端服务'
    return error.message || `请求失败（HTTP ${error.status}）`
  }
  return error instanceof Error ? error.message : String(error)
}

export type Query = Record<string, string | number | boolean | null | undefined>

function query(params: Query = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = authHeaders(init)

  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, { ...init, headers })
  } catch {
    throw new ApiError(0, 0, '无法连接后端服务')
  }
  let payload: Envelope<T>
  try {
    payload = (await response.json()) as Envelope<T>
  } catch {
    catchAuth(response.status)
    throw new ApiError(response.status, 0, `服务返回了无法解析的数据（HTTP ${response.status}）`)
  }
  if (!response.ok || payload.code !== 0) {
    catchAuth(response.status)
    throw new ApiError(response.status, payload.code, payload.message || `请求失败（HTTP ${response.status}）`)
  }
  return payload.data
}

const get = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal })

/**
 * /agent/runs 与 /agent/schedules 由网关 c.Data 原样透出内部服务的 JSON，
 * 不套 { code, message, data } 信封；这里单独走一条裸解析通道。
 */
async function rawRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = authHeaders(init)
  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, { ...init, headers })
  } catch {
    throw new ApiError(0, 0, '无法连接后端服务')
  }
  const text = await response.text()
  let payload: unknown = null
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    catchAuth(response.status)
    throw new ApiError(response.status, 0, '服务返回了无法解析的数据')
  }
  if (!response.ok) {
    catchAuth(response.status)
    const message = (payload as { message?: string } | null)?.message
    throw new ApiError(response.status, response.status, message || `请求失败（HTTP ${response.status}）`)
  }
  return payload as T
}

const rawPost = <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
  rawRequest<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}), headers })
const rawPut = <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
  rawRequest<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}), headers })

const post = <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}), headers })
const del = <T>(path: string) => request<T>(path, { method: 'DELETE' })

/** CSV 下载：后端在 Content-Type 非 text/csv 时视为失败，避免把错误页存成 .csv。 */
export async function download(path: string, filename: string): Promise<void> {
  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, { headers: authHeaders() })
  } catch {
    throw new ApiError(0, 0, '无法连接后端服务')
  }
  if (!response.ok || !response.headers.get('Content-Type')?.toLowerCase().includes('text/csv')) {
    catchAuth(response.status)
    let message = `后端未返回 CSV（HTTP ${response.status}）`
    try {
      const payload = (await response.json()) as Envelope<unknown>
      message = payload.message || message
    } catch {
      /* 保留状态码文案 */
    }
    throw new ApiError(response.status, 0, message)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export interface Page<T> {
  list: T[]
  page: number
  page_size: number
  total: number
}

/* ---------------- 看板 ---------------- */

export type Scope = 'all' | 'business' | 'baseline'

/** GET /dashboard/summary —— 字段与 handlers.go dashboard() 的 gin.H 完全一致 */
export interface Summary {
  device_total: number
  online: number
  offline: number
  telemetry_unknown: number
  fault: number
  current_power_kw: number
  active_alarms: number
  operational_health_percent: number
  health_score_percent: number | null
  health_valid_count: number
  health_unknown_count: number
  health_provisional: boolean
  health_rule_version: string
  health_computed_at: string
  today_energy_kwh: number | null
  retained_energy_kwh: number | null
  energy_provisional: boolean
  today_energy_provisional: boolean
  retained_energy_provisional: boolean
  energy_start_ms: number | null
  energy_updated_at: string | null
}

/** GET /dashboard/overview 的 stations[] —— 每站是 summary 的超集 */
export interface StationSummary extends Summary {
  station_code: string
  station_name: string
  scope: Exclude<Scope, 'all'>
  expected_device_count: number
  registered_device_count: number
  observed_device_count: number
  partial: boolean
}

export interface Overview {
  scope: Scope
  station_code: string
  computed_at: string
  partial: boolean
  expected_device_count: number
  registered_device_count: number
  observed_device_count: number
  health_rule_version: string
  summary: Summary
  stations: StationSummary[]
}

/** GET /stations —— 场站目录，registered 来自台账计数 */
export interface StationRow {
  station_code: string
  station_name: string
  scope: Exclude<Scope, 'all'>
  kind: string
  expected_device_count: number
  registered_device_count: number
}

/* ---------------- 设备与遥测 ---------------- */

export interface Device {
  id: number
  device_code: string
  name: string
  dev_type: string
  vendor: string
  station_code: string
  group_name: string
  is_simulated: boolean
  reference_vendor: string
  reference_model: string
  created_at: string
  updated_at: string
}

export interface Telemetry {
  device_id: number
  device_code: string
  run_id: string
  seq: number
  ts_ms: number
  received_at: string
  voltage: number
  current: number
  temperature: number
  power: number
  status: number
  fault_code: number
}

export type Metric = 'voltage' | 'current' | 'temperature' | 'power'

/** GET /devices/:id/telemetry —— points 是 [ts_ms, value]，TDengine 最多返回 5000 点 */
export interface History {
  device_id: number
  metric: Metric
  points: [number, number][]
}

/** GET /devices/:id/telemetry/progress —— 收件箱审计投影（不是完整历史） */
export interface TelemetryReceipt {
  inbox_id: string
  ts_ms: number
  received_at: string
  processed_at: string | null
  business_state: string | null
  td_confirmed_at: string | null
  alarm_evaluation: string | null
  failure_code: string | null
}

export interface TelemetryProgress {
  next_before_id: string | null
  device_id: number
  limit: number
  window: string
  rows: TelemetryReceipt[]
  historical_alarm_policy: string
  snapshot_business_commit: string
}

/* ---------------- 告警 ---------------- */

export type AlarmLevel = 'urgent' | 'major' | 'minor'
export type AlarmStatus = 'unhandled' | 'acked' | 'recovered'

export interface Alarm {
  id: number
  device_id: number
  rule_id: number | null
  metric: string
  level: AlarmLevel
  value: number
  threshold: number
  status: AlarmStatus
  acked_by: number | null
  acked_at: string | null
  recovered_at: string | null
  triggered_at: string
  archived: boolean
  origin: 'normal' | 'simulation'
  scenario_id: number | null
  close_reason: string | null
  rule_operator: string
  rule_version: string
}

export interface AlarmFilter {
  page?: number
  pageSize?: number
  status?: AlarmStatus | ''
  level?: AlarmLevel | ''
  origin?: 'normal' | 'simulation' | ''
  scope?: Scope
  stationCode?: string
  stationCodes?: string
  deviceId?: number
  start?: string
  end?: string
}

/** GET /alarms/statistics —— group_by 只允许 device/metric/level/status/origin */
export interface AlarmStatisticsGroup {
  key: string
  device_id?: number
  device_code?: string
  count: number
}

export interface AlarmStatistics {
  group_by: string
  start: string
  end: string
  total_events: number
  groups: AlarmStatisticsGroup[]
  complete: boolean
  counting_basis: string
  status_filter: string
  origin_filter: string
  device_id: number
  device_scope: string
}

/* ---------------- 健康 ---------------- */

export interface HealthRow {
  rank: number | null
  device_id: number
  device_code: string
  name: string
  station_code: string
  score: number | null
  state: string
  active_level: string | null
  status: number | null
  sampled_at_ms: number | null
  received_at: string | null
  matched_by: string
  rule_version: string
}

export interface StationHealthRow {
  rank: number | null
  station_code: string
  score: number | null
  provisional: boolean
  device_total: number
  valid_count: number
  unknown_count: number
  rule_version: string
}

export interface HealthBoard {
  rule_version: string
  computed_at: string
  complete: boolean
  basis: string
  devices: HealthRow[]
  stations: StationHealthRow[]
}

/* ---------------- 预测 ---------------- */

export interface Factor {
  feature: string
  value: number
  contribution: number
}

export interface Prediction {
  device_id: number
  window_end_ms: number | null
  probability: number | null
  threshold: number | null
  risk_level: 'high' | 'low' | null
  model_version: string | null
  top_factors: Factor[]
  source: string | null
  created_at: string | null
  stale: boolean
}

export interface PredictionRow extends Prediction {
  device_code: string
  name: string
  station_code: string
  group_name: string
}

/* ---------------- 报表 ---------------- */

export interface AvailabilityItem {
  device_id: number
  device_code: string
  station_code: string
  covered_ms: number
  window_ms: number
  running_ms: number
  stopped_ms: number
  fault_ms: number
  unknown_ms: number
  observed_availability: number | null
  data_coverage: number | null
  insufficient_data: boolean
  rule_version: string
}

export interface StationAvailabilityRow {
  station_code: string
  device_count: number
  running_ms: number
  stopped_ms: number
  fault_ms: number
  unknown_ms: number
  covered_ms: number
  window_ms: number
  non_fault_ms: number
  observed_availability: number | null
  data_coverage: number | null
  insufficient_data: boolean
}

export interface AvailabilityReport {
  rule_version: string
  computed_at: string
  start: string
  end: string
  devices: AvailabilityItem[]
  stations: StationAvailabilityRow[]
}

/** /reports/* 的公共查询参数：窗口必须是 RFC3339，后端按 station_code / device_id 过滤 */
export type ReportParams = Query & {
  start?: string
  end?: string
  device_id?: number
  station_code?: string
}

export interface TimelinessStat {
  count: number
  mean_ms: number | null
  median_ms: number | null
  p90_ms: number | null
}

export interface TimelinessResult {
  scope: string
  ack_timeliness: TimelinessStat
  recover_timeliness: TimelinessStat
  pending_ack: number
  pending_recover: number
  managed_or_unknown_recover: number
  telemetry_recover_timeliness: TimelinessStat
  prediction_recover_timeliness: TimelinessStat
  managed_recover: number
  unknown_recover: number
  event_count: number
  invalid_lifecycle: number
  recovery_reasons: Record<string, number>
}

export interface TimelinessReport {
  rule_version: string
  computed_at: string
  start: string
  end: string
  scope: string
  result: TimelinessResult
}

/* ---------------- 定期任务（09 ops agent，经后端代理） ---------------- */

export type ScheduleStatus = 'active' | 'paused' | 'cancelled'

export interface ScheduleConfig {
  task_type: 'daily_report' | 'inspection'
  scope: Scope
  station_code: string | null
  device_id: number | null
  timezone: string
  mode: 'daily' | 'interval'
  daily_time: string | null
  interval_minutes: number | null
  grant_days: number
  destination_id: 'platform_client'
  destination_version: '1'
}

export interface Schedule {
  id: string
  config_version: number
  config: ScheduleConfig
  status: ScheduleStatus
  next_run_at: string | null
  reason: string | null
  grant: { grant_id: string; expires_at: string; revoked_at: string | null } | null
}

export interface ScheduleRunBrief {
  run_id: string
  status: string
  capability: string
}

export interface ScheduleBoard {
  list: Schedule[]
  runs: ScheduleRunBrief[]
}

export interface ScheduleHistoryRow {
  scheduled_for: string
  run_id: string | null
  run_status: string | null
  status: 'delivered' | 'skipped'
  reason: string | null
  skipped_count: number
}

/** 定期任务的写操作回执：相当于一次运行受理（运行本身走 agent-runs.ts） */
export interface RunReceipt {
  run_id: string
  session_id?: string
  turn_id?: number | string
  status: string
}

/**
 * 方案预演入参（对齐 08 `Preview` 模型）：单设备、带时区且不超过 24 小时的**历史**窗口、
 * 整数阈值；`expected_interval_seconds` 是本人假设的采样间隔，缺测口径由它决定。
 */
export interface RulePreviewInput {
  device_id: number
  metric: 'voltage' | 'current' | 'temperature' | 'power'
  start: string
  end: string
  operator: 'gt' | 'gte' | 'lt' | 'lte'
  threshold: number
  baseline_threshold: number
  expected_interval_seconds: number
}

/** `rule-preview.json` 产物：比较采样命中，不解释为收益、漏报率或线上规则效果 */
export interface RulePreviewResult {
  mode: 'simulation'
  parameters: RulePreviewInput
  snapshot_sha256: string
  coverage: {
    start_inclusive: string
    end_exclusive: string
    valid_samples: number
    expected_samples: number
    missing_samples: number
    invalid_or_duplicate_or_outside: number
    interior_gap_samples: number
    complete: boolean
  }
  metrics: {
    baseline_hits: number
    candidate_hits: number
    new_hits: number
    baseline_only_hits: number
  }
  assumptions: string[]
  summary: string
}

/* ---------------- 智能助手 ---------------- */

/** GET /agent/sessions 的列表项（RAG 服务字段名是 id） */
export interface AgentSession {
  id: string
  title: string
  created_at: string
  updated_at: string
}

/**
 * 一轮回答引用的来源（RAG 服务 sources[]）。
 * kind = document 走知识库章节（可读全文），web 是公网检索页，business / infrastructure
 * 是平台自有数据与取证结果，只有编号与摘要。
 */
export interface RAGSource {
  id: string
  kind: 'business' | 'document' | 'web' | 'infrastructure'
  title: string
  url: string
  chapter?: string
  version?: string
  page?: number | null
  published_at?: string | null
  collected_at: string
  summary?: string
}

/** GET /agent/knowledge/:id —— 知识库章节正文 */
export interface KnowledgeChapter {
  id: string
  document: string
  heading: string
  content: string
  version: string
  authority?: string
  page?: number | null
}

/**
 * GET /agent/sessions/:id 的 turn.response。
 *
 * RAG 服务返回的字段远多于界面用到的，这里只声明有渲染落点的那几个；
 * 其余由界面忽略，不猜字段含义。
 */
export interface AgentTurnResponse {
  answer_markdown?: string
  status?: string
  model?: string
  request_id?: string
  sources?: RAGSource[]
  evidence?: { state: string; success_count: number; failed_count: number; cards: unknown[] }
  warnings?: string[]
  response_mode?: 'model' | 'preset' | 'boundary' | 'task_dispatched'
  model_called?: boolean
  verification_available?: boolean
  kind?: 'chat' | 'verification'
}

export interface AgentTurn {
  id?: number
  run_id?: string | null
  question: string
  response?: AgentTurnResponse | null
  created_at?: string
}

/* ---------------- 告警解读（/agent/interpretations，生产跑 v2） ---------------- */

/**
 * 解读是**后端异步任务**，不是「请求就有答案」：
 * interpretationScanner 扫到新告警建一行（pending）→ interpretationWorker 租约加锁调模型 →
 * 结果写进 result，task_status 落到 completed / degraded。
 * 所以刚触发的告警常常读到 task_status=pending、result.conclusion=null —— 界面要按「生成中」处理，
 * 不能当成「这条告警没有解读」。
 */
export type InterpretationTaskStatus = 'pending' | 'running' | 'completed' | 'degraded'
export type InterpretationCategory = 'device_alarm' | 'prediction_risk' | 'platform_monitor'
/** answered=模型给出结论；unable_to_determine=证据不足明确拒答；degraded=没跑通（见 reason） */
export type InterpretationStatus = 'answered' | 'degraded' | 'unable_to_determine'

/** v2 的 analysis 是模型原始输出：{{F1}} 占位 + fact_refs，由 Go 渲染成 text 后写回 conclusion/findings */
export interface InterpretationAnalysisRef {
  template: string
  fact_refs: string[]
}

export interface InterpretationAnalysis {
  findings?: InterpretationAnalysisRef[]
  hypotheses?: InterpretationAnalysisRef[]
  limitations?: InterpretationAnalysisRef[]
  suggestions?: InterpretationAnalysisRef[]
}

/** Go 侧从 evidence 提取的权威事实（不是模型编的），fact_id 形如 F1，与 evidence_id 可互查 */
export interface InterpretationFact {
  fact_id: string
  label: string
  value: unknown
  display_value: string
  path: string
  role: string
  unit?: string
  value_type?: string
  precision?: number
  timezone?: string
  evidence_id?: string
}

export interface InterpretationEvidence {
  id: string
  tool?: string
  source?: string
  status?: string
  data?: unknown
  data_time?: string
  collected_at?: string
}

/** v1/v2 共有的渲染落点；v2 另给 analysis_status / facts / hypotheses / analysis */
export interface InterpretationResult {
  protocol_version?: number
  event_key?: string
  status?: InterpretationStatus
  analysis_status?: string
  conclusion?: { text: string; evidence_ids?: string[] } | null
  findings?: { text: string; evidence_ids?: string[] }[]
  suggestions?: { text: string; evidence_ids?: string[]; equipment_operation?: boolean }[]
  hypotheses?: { text: string; evidence_ids?: string[] }[]
  limitations?: string[]
  facts?: InterpretationFact[]
  evidence?: InterpretationEvidence[]
  analysis?: InterpretationAnalysis | null
  model?: string
  reason?: string
  error_code?: string
  retryable?: boolean
  retry_after?: number
  evidence_hash?: string
  request_id?: string
  diagnostic?: Record<string, unknown>
}

export interface Interpretation {
  id: number
  event_key: string
  category: InterpretationCategory
  alarm_id: number | null
  level: AlarmLevel | null
  occurred_at: string
  task_status: InterpretationTaskStatus
  reason: string
  result: InterpretationResult | null
  /** Go 落库的证据快照（权威，不是模型输出）；列表与详情都会带上 */
  evidence: InterpretationEvidence[] | null
  updated_at: string
  /** 当前登录用户是否已读（读接口会写 agent_interpretation_reads） */
  read: boolean
  /** 事件是否仍未恢复；false 表示已经过去，可按「历史」展示 */
  active: boolean
  alarm?: Alarm | null
  monitor?: Record<string, unknown> | null
}

/* ---------------- 端点 ---------------- */

const DAY = 86_400_000

export const api = {
  /* 身份：登录不走 request()，因为它的 401 会被当成“会话失效”清空并跳转；
     登录页的 401 是“账号密码错”，要留在原页显示错误。 */
  login: async (username: string, password: string): Promise<LoginResult> => {
    let response: Response
    try {
      response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
    } catch {
      throw new ApiError(0, 0, '无法连接后端服务')
    }
    let payload: Envelope<LoginResult>
    try {
      payload = (await response.json()) as Envelope<LoginResult>
    } catch {
      throw new ApiError(response.status, 0, `服务返回了无法解析的数据（HTTP ${response.status}）`)
    }
    if (response.status === 401) throw new ApiError(401, payload.code || 40101, '账号或密码不正确')
    if (!response.ok || payload.code !== 0) {
      throw new ApiError(response.status, payload.code, payload.message || '登录失败')
    }
    return payload.data
  },

  /** 免鉴权链路探针：登录页用它显示“数据链路正常/异常” */
  ping: () =>
    get<{ status: string; telemetry_ready: boolean; telemetry_phase: string }>('/ping'),

  /* 看板 */
  summary: () => get<Summary>('/dashboard/summary'),
  overview: (scope: Scope = 'all', stationCode = '') =>
    get<Overview>(`/dashboard/overview${query({ scope, station_code: stationCode })}`),
  stations: (scope: Scope = 'all') => get<{ scope: Scope; list: StationRow[]; total: number }>(`/stations${query({ scope })}`),

  /* 设备与遥测 */
  devices: (filter: { page?: number; pageSize?: number; groupName?: string; scope?: Scope; stationCode?: string; keyword?: string } = {}) =>
    get<Page<Device>>(
      `/devices${query({
        page: filter.page ?? 1,
        page_size: filter.pageSize ?? 20,
        group_name: filter.groupName,
        scope: filter.scope,
        station_code: filter.stationCode,
        keyword: filter.keyword,
      })}`,
    ),
  device: (id: number) => get<Device>(`/devices/${id}`),
  latest: (id: number) => get<Telemetry>(`/devices/${id}/telemetry/latest`),
  /** 窗口必须 ≤ 24h 且点数 ≤ 5000，超限由后端 400/503 拒绝 */
  history: (id: number, metric: Metric, start: Date, end: Date, signal?: AbortSignal) =>
    get<History>(
      `/devices/${id}/telemetry${query({ metric, start: start.toISOString(), end: end.toISOString() })}`,
      signal,
    ),
  telemetryProgress: (id: number, beforeId?: string) =>
    get<TelemetryProgress>(`/devices/${id}/telemetry/progress${query({ before_id: beforeId })}`),
  exportTelemetry: (id: number, start: Date, end: Date) =>
    download(
      `/devices/${id}/telemetry/export${query({ start: start.toISOString(), end: end.toISOString() })}`,
      `device-${id}-telemetry.csv`,
    ),

  /* 告警 */
  alarms: (filter: AlarmFilter = {}) =>
    get<Page<Alarm>>(
      `/alarms${query({
        page: filter.page ?? 1,
        page_size: filter.pageSize ?? 20,
        status: filter.status,
        level: filter.level,
        origin: filter.origin,
        scope: filter.scope,
        station_code: filter.stationCodes || filter.stationCode,
        device_id: filter.deviceId,
        start: filter.start,
        end: filter.end,
      })}`,
    ),
  alarm: (id: number) => get<Alarm>(`/alarms/${id}`),
  ackAlarm: (id: number) => post<Alarm>(`/alarms/${id}/ack`),
  archiveAlarm: (id: number) => post<Alarm>(`/alarms/${id}/archive`),
  /** 批量归档：后端只改 archived 标记，不存在或非法的编号回在 failed 里，不阻断其余 */
  archiveAlarms: (ids: number[]) =>
    post<{ archived: number; failed: number[]; updated_at: string }>('/alarm-archive', { ids }),
  alarmStatistics: (groupBy: 'device' | 'metric' | 'level' | 'status' | 'origin' = 'level') =>
    get<AlarmStatistics>(`/alarms/statistics${query({ group_by: groupBy })}`),

  /* 告警解读：后端异步任务，按 alarm_id 取一条即可；attention=true 只回未读且仍值得看的 */
  interpretations: (
    filter: {
      page?: number
      pageSize?: number
      category?: InterpretationCategory
      alarmId?: number
      updatedAfter?: string
      attention?: boolean
    } = {},
    signal?: AbortSignal,
  ) =>
    get<Page<Interpretation>>(
      `/agent/interpretations${query({
        page: filter.page ?? 1,
        page_size: filter.pageSize ?? 20,
        category: filter.category,
        alarm_id: filter.alarmId,
        updated_after: filter.updatedAfter,
        attention: filter.attention ? 'true' : undefined,
      })}`,
      signal,
    ),
  interpretation: (id: number, signal?: AbortSignal) =>
    get<Interpretation>(`/agent/interpretations/${id}`, signal),
  /** 标记已读：只影响当前用户（agent_interpretation_reads），不改解读本身 */
  readInterpretation: (id: number) => post<{ id: number; read: boolean }>(`/agent/interpretations/${id}/read`),

  /* 健康 */
  healthDevices: () => get<HealthBoard>('/health/devices'),
  healthStations: () => get<{ rule_version: string; computed_at: string; stations: StationHealthRow[]; complete: boolean; basis: string }>('/health/stations'),

  /* 预测 */
  predictions: (page = 1, pageSize = 20) => get<Page<PredictionRow>>(`/predictions${query({ page, page_size: pageSize })}`),
  prediction: (id: number) => get<Prediction>(`/devices/${id}/prediction`),

  /* 报表：JSON 与 CSV 同一路由，后端 availability()/alarmTimeliness() 命中 ?format=csv 时走
     report_export.go 的 writeReportCSV（含场站汇总行，Content-Type=text/csv），比前端拼当前页更全 */
  availability: (params: ReportParams = {}, signal?: AbortSignal) =>
    get<AvailabilityReport>(`/reports/availability${query(params)}`, signal),
  timeliness: (params: ReportParams = {}, signal?: AbortSignal) =>
    get<TimelinessReport>(`/reports/alarm-timeliness${query(params)}`, signal),
  /** 后端 CSV 导出：与查询同参数 + format=csv，返回 text/csv 文件流 */
  exportReport: (kind: 'availability' | 'alarm-timeliness', params: ReportParams = {}) =>
    download(`/reports/${kind}${query({ ...params, format: 'csv' })}`, `${kind}.csv`),

  /* 定期任务（网关原样透出，无信封） */
  schedules: () => rawRequest<ScheduleBoard>('/agent/schedules'),
  scheduleHistory: (id: string, offset = 0) =>
    rawRequest<{ list: ScheduleHistoryRow[] }>(`/agent/schedules/${encodeURIComponent(id)}/history${query({ offset })}`),
  scheduleAction: (item: Schedule, action: 'pause' | 'resume' | 'cancel') =>
    rawPost<Schedule | RunReceipt>(
      `/agent/schedules/${encodeURIComponent(item.id)}/${action}`,
      action === 'resume' ? { expected_version: item.config_version } : {},
      { 'Idempotency-Key': crypto.randomUUID() },
    ),
  createSchedule: (config: ScheduleConfig) =>
    rawPost<RunReceipt>('/agent/schedules', { config }, { 'Idempotency-Key': crypto.randomUUID() }),
  updateSchedule: (item: Schedule, config: ScheduleConfig) =>
    rawPut<RunReceipt>(
      `/agent/schedules/${encodeURIComponent(item.id)}`,
      { config, expected_version: item.config_version },
      { 'Idempotency-Key': crypto.randomUUID() },
    ),
  /** 只读历史预演：受理后返回运行回执，结果在运行的 `rule-preview.json` 产物里 */
  previewRule: (input: RulePreviewInput) =>
    rawPost<RunReceipt>('/agent/rule-preview', input, { 'Idempotency-Key': crypto.randomUUID() }),

  /* 智能助手：会话与来源走信封；运行与定期任务走裸 JSON，见 agent-runs.ts */
  sessions: (offset = 0) =>
    get<{ list: AgentSession[]; offset: number; page_size: number }>(`/agent/sessions${query({ offset })}`),
  session: (id: string) => get<{ session_id: string; turns: AgentTurn[] }>(`/agent/sessions/${encodeURIComponent(id)}`),
  deleteSession: (id: string) => del<{ deleted: boolean }>(`/agent/sessions/${encodeURIComponent(id)}`),
  knowledge: (id: string) => get<KnowledgeChapter>(`/agent/knowledge/${encodeURIComponent(id)}`),

  /* 实时通道 */
  ticket: () => post<{ ticket: string; expires_in: number }>('/ws-ticket'),

  /* 运维任务（09 ops-agent）：列表/详情/取消/删除/审批决定，与 PowerSystem 原前端同名同参 */
  opsTasks: (
    sessionId?: string,
    offset = 0,
    options: { status?: OpsStatus; limit?: number; signal?: AbortSignal } = {},
  ) =>
    get<{ list: OpsTask[]; offset: number; page_size: number }>(
      `/agent/tasks${query({ session_id: sessionId, offset, status: options.status, limit: options.limit })}`,
      options.signal,
    ),
  opsTask: (id: string, signal?: AbortSignal) =>
    get<OpsTask>(`/agent/tasks/${encodeURIComponent(id)}`, signal),
  cancelOpsTask: (id: string) => post<OpsTask>(`/agent/tasks/${encodeURIComponent(id)}/cancel`),
  deleteOpsTask: (id: string) =>
    request<{ task_id: string; deleted: boolean; already_deleted: boolean; deleted_at: string }>(
      `/agent/tasks/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  decideOpsApproval: (id: string, approvalID: string, decision: 'approve' | 'reject') =>
    post<OpsTask>(
      `/agent/tasks/${encodeURIComponent(id)}/approvals/${encodeURIComponent(approvalID)}/decision`,
      { decision },
    ),
}

export const WEEK_MS = 7 * DAY
