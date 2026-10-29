/**
 * V4-S04 运行客户端（迁自 stages/04-frontend 覆盖前的 lib/agent-runs.ts）。
 *
 * Go 网关对 `/agent/runs*` 是 `c.Data(...)` 原样透传内部服务，因此这些接口返回
 * 裸 JSON，不是 `{ code, message, data }` 信封。所以本模块用私有 fetch，不走
 * `api.ts` 的 request()：同样的 Authorization 头、同样的 401 出口、同样的 HTTP
 * 错误语义，但按裸 JSON 解析。
 *
 * `seq` / `turn_id` / `last_seq` 在服务端是 64 位整数。解析前先把 16 位以上
 * 的整数字面量加引号，避免超过 2^53 的值在 JS number 里丢精度；能安全表示的
 * 仍然返回 number。
 *
 * JWT 只走 Authorization 头，绝不进 URL（EventSource 无法自定义头，所以流式
 * 读取用 fetch + ReadableStream 自己解 SSE，而不是 EventSource）。
 */
import { authToken, notifyUnauthorized } from './auth'

const PROTOCOL = 'assistant/v4-draft1'
const MAX_BACKOFF_MS = 8000
const MAX_RECOVERIES = 3

export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'reconciling'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'pending_reconciliation'

/** 精确 64 位整数：安全范围内是 number，否则是原始字面量。 */
export type Seq = string | number

export type RunConnectionState = 'connecting' | 'live' | 'reconnecting' | 'stopped'

export type RunEventType =
  | 'accepted'
  | 'progress'
  | 'text_delta'
  | 'tool_started'
  | 'tool_finished'
  | 'approval_required'
  | 'action_blocked'
  | 'artifact_ready'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface RunInput {
  message: string
  session_id?: string
  page_context?: Record<string, unknown>
}

export interface RunReceipt {
  run_id: string
  turn_id: Seq
  session_id: string
  status: RunStatus
}

export interface ResultRef {
  task_id: string
  status: string
  historical?: boolean
  [key: string]: unknown
}

export interface RunAction {
  action_id?: string
  status?: string
  approval_id?: string
  plan_hash?: string
  expires_at?: string
  risk_class?: string
  effect_summary?: string
  target_summary?: string
  authority_revision?: number
  observed_at?: string
  stale?: boolean
  [key: string]: unknown
}

export interface RunArtifact {
  artifact_id?: string
  name?: string
  mime_type?: string
  size?: number
  sha256?: string
  expires_at?: string
  [key: string]: unknown
}

export interface RunError {
  code?: string
  message?: string
  retryable?: boolean
}

export interface RunEvent {
  protocol: string
  run_id: string
  turn_id: Seq
  seq: Seq
  type: RunEventType
  at: string
  data: Record<string, unknown>
}

/**
 * `GET /api/v1/agent/runs/:id` 的投影。字段集由 v4 契约固定；
 * 客户端整体替换快照，不做局部修补。
 */
export interface RunSnapshot {
  authority_sync_status?: 'pending_reconciliation'
  protocol: string
  run_id: string
  session_id: string
  turn_id: Seq
  status: RunStatus
  phase: string
  cancel_requested: boolean
  requires_manual_reconciliation: boolean
  final_answer: string
  answer_version: number
  result_refs: ResultRef[]
  actions: RunAction[]
  artifacts: RunArtifact[]
  terminal_event: RunEvent | null
  last_seq: Seq
  error: RunError | null
  progress_message?: string
}

export class RunHttpError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super(code)
    this.name = 'RunHttpError'
  }
}

export class RunProtocolError extends Error {
  constructor(public readonly code: string) {
    super(code)
    this.name = 'RunProtocolError'
  }
}

const EVENT_TYPES: readonly RunEventType[] = [
  'accepted', 'progress', 'text_delta', 'tool_started', 'tool_finished',
  'approval_required', 'action_blocked', 'artifact_ready', 'completed', 'failed', 'cancelled',
]

const EVENT_DATA_FIELDS: Record<RunEventType, readonly string[]> = {
  accepted: ['status', 'session_id', 'turn_id'],
  progress: ['phase', 'message', 'percent'],
  text_delta: ['answer_version', 'offset', 'text', 'reset'],
  tool_started: ['call_id', 'capability', 'status', 'summary', 'result_ref', 'error'],
  tool_finished: ['call_id', 'capability', 'status', 'summary', 'result_ref', 'error'],
  approval_required: ['action_id', 'approval_id', 'plan_hash', 'expires_at', 'risk_class', 'effect_summary', 'target_summary', 'authority_revision', 'observed_at', 'stale'],
  action_blocked: ['action_id', 'rule_codes', 'reason'],
  artifact_ready: ['artifact_id', 'name', 'mime_type', 'size', 'sha256', 'expires_at'],
  completed: ['status', 'answer_version', 'result_refs', 'error', 'requires_manual_reconciliation'],
  failed: ['status', 'answer_version', 'result_refs', 'error', 'requires_manual_reconciliation'],
  cancelled: ['status', 'answer_version', 'result_refs', 'error', 'requires_manual_reconciliation'],
}

const ACTION_FIELDS = [
  'action_id', 'status', 'approval_id', 'plan_hash', 'expires_at', 'risk_class',
  'effect_summary', 'target_summary', 'authority_revision', 'observed_at', 'stale',
  'target_version', 'capability', 'error_code', 'display_result', 'execution_ref', 'consumed_at',
  'protocol', 'policy_version', 'destination_id', 'recovery_limit', 'health_check', 'action_confirmed', 'parameters_summary',
] as const

const ARTIFACT_FIELDS = ['artifact_id', 'name', 'mime_type', 'size', 'sha256', 'expires_at', 'status'] as const

/** 调用方声明并须在后续每次读取中持续匹配的绑定。 */
interface SnapshotExpectation {
  protocol?: string
  run_id?: string
  session_id?: string
  turn_id?: Seq
}

export async function createRun(input: RunInput, key: string, signal?: AbortSignal): Promise<RunReceipt> {
  const raw = await runJson('/api/v1/agent/runs', {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(input),
    signal,
  })
  return parseReceipt(raw)
}

export async function getRun(id: string, signal?: AbortSignal): Promise<RunSnapshot> {
  return readSnapshot(id, { run_id: id }, signal)
}

/** 只返回服务端真实快照，不臆造终态。 */
export async function cancelRun(id: string, signal?: AbortSignal): Promise<RunSnapshot> {
  const raw = await runJson(`/api/v1/agent/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST', signal })
  return normalizeSnapshot(raw, { run_id: id })
}

export async function decideAction(
  run: RunSnapshot,
  actionId: string,
  approvalId: string,
  planHash: string,
  decision: 'approve' | 'reject',
): Promise<RunSnapshot> {
  const identity = authToken.value
  const raw = await runJson(
    `/api/v1/agent/runs/${encodeURIComponent(run.run_id)}/actions/${encodeURIComponent(actionId)}/decision`,
    { method: 'POST', body: JSON.stringify({ decision, approval_id: approvalId, plan_hash: planHash }) },
  )
  if (authToken.value !== identity) throw new RunProtocolError('identity_changed')
  return normalizeSnapshot(raw, { run_id: run.run_id, session_id: run.session_id, turn_id: run.turn_id })
}

/**
 * 单个运行快照的纯 reducer。它不碰会话/轮次历史，也不修改入参。未知字段与未知
 * 事件类型一律丢弃，保证原始工具载荷或内部推理不会漏到工作台。
 */
export function applyRunEvent(snapshot: RunSnapshot, event: RunEvent): RunSnapshot {
  if (event.protocol !== snapshot.protocol) throw new RunProtocolError('protocol_mismatch')
  if (event.run_id !== snapshot.run_id) throw new RunProtocolError('run_binding_mismatch')
  if (String(event.turn_id) !== String(snapshot.turn_id)) throw new RunProtocolError('turn_binding_mismatch')
  if (!isRunEventType(event.type)) throw new RunProtocolError('unknown_event_type')

  const seq = toBigInt(event.seq)
  const last = toBigInt(snapshot.last_seq)
  if (seq <= last) return snapshot
  if (seq !== last + 1n) throw new RunProtocolError('seq_gap')

  const next: RunSnapshot = { ...snapshot, last_seq: event.seq }
  // 解析器里已经洗过一遍，这里再洗一次：导出的 reducer 也不能成为泄漏通道。
  const data = sanitizeData(event.type, event.data)

  switch (event.type) {
    case 'accepted': {
      const session = text(data.session_id)
      const turn = data.turn_id
      if (session !== undefined && session !== snapshot.session_id) throw new RunProtocolError('session_binding_mismatch')
      if (turn !== undefined && String(turn) !== String(snapshot.turn_id)) throw new RunProtocolError('turn_binding_mismatch')
      const status = asStatus(data.status)
      if (status) next.status = status
      break
    }
    case 'progress': {
      const phase = text(data.phase)
      if (phase) next.phase = phase
      if (phase === 'starting' && next.status === 'queued') next.status = 'running'
      if (phase === 'reconciling') next.status = 'reconciling'
      if (phase === 'authority_updated' && next.status === 'awaiting_approval') next.status = 'running'
      next.progress_message = text(data.message)
      break
    }
    case 'tool_started':
    case 'tool_finished':
      next.phase = event.type === 'tool_started' ? 'tool' : 'model'
      next.progress_message = [text(data.capability), text(data.summary)].filter(Boolean).join(' · ')
      break
    case 'text_delta': {
      const version = integer(data.answer_version)
      const offset = integer(data.offset)
      const delta = text(data.text)
      if (version === undefined || offset === undefined || delta === undefined) throw new RunProtocolError('invalid_text_delta')
      const applied = applyText(snapshot.final_answer, snapshot.answer_version, version, offset, delta, data.reset === true)
      next.final_answer = applied.text
      next.answer_version = applied.version
      break
    }
    case 'approval_required': {
      const action = normalizeAction(data)
      if (action?.action_id) next.actions = upsertAction(snapshot.actions, action)
      next.status = 'awaiting_approval'
      break
    }
    case 'artifact_ready': {
      const artifact = normalizeArtifact(data)
      if (artifact?.artifact_id) next.artifacts = upsertArtifact(snapshot.artifacts, artifact)
      break
    }
    case 'completed':
    case 'failed':
    case 'cancelled': {
      const status = asStatus(data.status)
        ?? (event.type === 'completed' ? 'succeeded' : event.type === 'failed' ? 'failed' : 'cancelled')
      next.status = status
      const version = integer(data.answer_version)
      if (version !== undefined) next.answer_version = version
      if (Array.isArray(data.result_refs)) next.result_refs = normalizeRefs(data.result_refs)
      if (typeof data.requires_manual_reconciliation === 'boolean') next.requires_manual_reconciliation = data.requires_manual_reconciliation
      next.error = normalizeError(data.error)
      next.terminal_event = { ...event, data }
      break
    }
    default:
      break
  }

  return next
}

/**
 * 消费一个运行的 SSE 流，直到终态或被取消。
 *
 * 快照由调用方持有；每次 `onSnapshot` 拿到的都是完整快照（不是局部补丁，也不是
 * 会话历史）。初始快照与之后的每次恢复读取都必须保持同一组
 * run/turn/session/protocol 绑定，否则直接中止，绝不显示别人的状态。
 *
 * 断线按有界退避重连，绝不取消运行本身。
 */
export async function consumeRun(
  id: string,
  snapshot: RunSnapshot,
  onSnapshot: (snapshot: RunSnapshot) => void,
  signal?: AbortSignal,
  onConnection?: (state: RunConnectionState) => void,
): Promise<void> {
  const initial = normalizeSnapshot(snapshot, { run_id: id })
  const expect: SnapshotExpectation = {
    protocol: initial.protocol,
    run_id: initial.run_id,
    session_id: initial.session_id,
    turn_id: initial.turn_id,
  }
  let current = initial
  let first = true
  let attempt = 0
  let recoveries = 0
  let progressed = false
  let stopped = false
  const stop = () => {
    if (stopped) return
    stopped = true
    onConnection?.('stopped')
  }

  try {
    if (isTerminalStatus(current.status)) {
      stop()
      return
    }
    while (!signal?.aborted) {
      onConnection?.(first ? 'connecting' : 'reconnecting')
      first = false
      progressed = false
      try {
        const outcome = await streamOnce(id, current, next => {
          progressed = true
          current = next
          onSnapshot(next)
        }, onConnection, signal)
        if (signal?.aborted) break
        if (outcome === 'terminal') {
          const final = await finalSnapshot(id, expect, signal)
          if (final) {
            current = final
            onSnapshot(final)
          }
          break
        }
        // 没有终态事件的正常关闭仍算断线：先读一次权威快照再重试，
        // 否则漏掉终态事件就会一直空转。
        const fresh = await readSnapshot(id, expect, signal)
        current = fresh
        onSnapshot(fresh)
        if (isTerminalStatus(fresh.status)) break
        attempt += 1
        await wait(backoffDelay(attempt), signal)
      } catch (error) {
        if (signal?.aborted) break
        const status = error instanceof RunHttpError ? error.status : undefined
        if (status === 401 || status === 403 || status === 404) throw error
        const cursorExpired = error instanceof RunHttpError && (status === 400 || status === 410)
        if (cursorExpired || error instanceof RunProtocolError) {
          // 只有「一直违约且一个事件都没产出」才算不可恢复；有进展就重置预算。
          if (progressed) recoveries = 0
          recoveries += 1
          if (recoveries > MAX_RECOVERIES) throw error
          const fresh = await readSnapshot(id, expect, signal)
          current = fresh
          onSnapshot(fresh)
          if (isTerminalStatus(fresh.status)) break
          attempt = 0
          continue
        }
        attempt += 1
        await wait(backoffDelay(attempt), signal)
      }
    }
  } finally {
    stop()
  }
}

interface SseFrame {
  id?: string
  event?: string
  data: string
}

async function streamOnce(
  id: string,
  start: RunSnapshot,
  emit: (snapshot: RunSnapshot) => void,
  onConnection: ((state: RunConnectionState) => void) | undefined,
  signal: AbortSignal | undefined,
): Promise<'terminal' | 'closed'> {
  const headers = new Headers({ Accept: 'text/event-stream' })
  if (authToken.value) headers.set('Authorization', `Bearer ${authToken.value}`)
  const response = await fetch(eventsUrl(id, start.last_seq), { method: 'GET', headers, signal, cache: 'no-store' })
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized()
    throw new RunHttpError(response.status, await errorCodeFromResponse(response))
  }
  const mediaType = (response.headers?.get('Content-Type') ?? '').toLowerCase()
  if (!mediaType.startsWith('text/event-stream')) throw new RunProtocolError('invalid_event_stream')
  onConnection?.('live')
  if (!response.body) throw new RunProtocolError('missing_event_body')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let current = start
  const consume = (raw: string): boolean => {
    const frame = parseSseFrame(raw)
    if (!frame) return false
    const event = parseRunEvent(frame)
    const next = applyRunEvent(current, event)
    if (next !== current) {
      current = next
      emit(next)
    }
    return isTerminalEvent(event.type) && isTerminalStatus(current.status)
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      for (let frame = takeFrame(buffer); frame; frame = takeFrame(buffer)) {
        buffer = frame.rest
        if (consume(frame.frame)) return 'terminal'
      }
    }
    buffer += decoder.decode()
    if (buffer.trim() && consume(buffer)) return 'terminal'
    return 'closed'
  } finally {
    try { await reader.cancel() } catch { /* 流已关闭 */ }
    reader.releaseLock()
  }
}

function parseSseFrame(raw: string): SseFrame | null {
  const data: string[] = []
  let id: string | undefined
  let event: string | undefined
  for (const line of raw.split(/\r\n|\n|\r/)) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') data.push(value)
    else if (field === 'event') event = value
    else if (field === 'id') id = value
  }
  if (!data.length) return null
  return { id, event, data: data.join('\n') }
}

function takeFrame(buffer: string): { frame: string; rest: string } | null {
  const match = /\r\n\r\n|\n\n|\r\r/.exec(buffer)
  if (!match) return null
  return { frame: buffer.slice(0, match.index), rest: buffer.slice(match.index + match[0].length) }
}

function parseRunEvent(frame: SseFrame): RunEvent {
  let parsed: Record<string, unknown>
  try {
    parsed = JSON.parse(quoteIntegers(frame.data)) as Record<string, unknown>
  } catch {
    throw new RunProtocolError('invalid_event_json')
  }
  if (!isRecord(parsed)) throw new RunProtocolError('invalid_event_json')
  const parsedSeq = typeof parsed.seq === 'string'
    ? parsed.seq
    : typeof parsed.seq === 'number' && Number.isInteger(parsed.seq) ? String(parsed.seq) : undefined
  const frameId = frame.id && frame.id.length ? frame.id : undefined
  if (frameId !== undefined && parsedSeq !== undefined && !sameInteger(frameId, parsedSeq)) throw new RunProtocolError('event_id_mismatch')
  const seqLexeme = frameId ?? parsedSeq
  if (seqLexeme === undefined) throw new RunProtocolError('missing_event_seq')
  const seq = seqValue(seqLexeme, 'invalid_event_seq')

  const type = (typeof parsed.type === 'string' ? parsed.type : frame.event) as RunEventType | undefined
  if (!type) throw new RunProtocolError('missing_event_type')
  if (!isRunEventType(type)) throw new RunProtocolError('unknown_event_type')
  if (frame.event !== undefined && frame.event !== type) throw new RunProtocolError('event_type_mismatch')

  return {
    protocol: typeof parsed.protocol === 'string' ? parsed.protocol : '',
    run_id: typeof parsed.run_id === 'string' ? parsed.run_id : '',
    turn_id: seqValue(parsed.turn_id, 'missing_turn_id'),
    seq,
    type,
    at: typeof parsed.at === 'string' ? parsed.at : '',
    data: sanitizeData(type, isRecord(parsed.data) ? parsed.data : {}),
  }
}

function sanitizeData(type: RunEventType, data: Record<string, unknown>): Record<string, unknown> {
  const allowed = EVENT_DATA_FIELDS[type]
  if (!allowed) return {}
  const result: Record<string, unknown> = {}
  for (const key of allowed) {
    if (!(key in data)) continue
    const value = sanitizeField(key, data[key])
    if (value !== undefined) result[key] = value
  }
  return result
}

function sanitizeField(key: string, value: unknown): unknown {
  if (key === 'error') return normalizeError(value)
  if (key === 'result_refs') return normalizeRefs(value)
  if (key === 'rule_codes') return Array.isArray(value) ? value.filter(item => typeof item === 'string') : undefined
  if (typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined
  return undefined
}

function applyText(current: string, currentVersion: number, version: number, offset: number, delta: string, reset: boolean): { text: string; version: number } {
  if (offset < 0 || version < currentVersion) throw new RunProtocolError('invalid_text_delta')
  let base = current
  if (reset) {
    if (offset !== 0) throw new RunProtocolError('reset_offset')
    base = ''
  } else if (version !== currentVersion) {
    // 版本变化必须从零重建，否则已存正文与新 delta 属于两份不同回答。
    throw new RunProtocolError('answer_version_mismatch')
  }

  const points = Array.from(base)
  if (offset > points.length) throw new RunProtocolError('answer_gap')
  const incoming = Array.from(delta)
  if (offset < points.length) {
    const overlap = points.slice(offset, offset + incoming.length)
    const prefix = incoming.slice(0, overlap.length)
    if (overlap.some((char, index) => char !== prefix[index])) throw new RunProtocolError('answer_offset_conflict')
    return { text: base + incoming.slice(overlap.length).join(''), version }
  }
  return { text: base + delta, version }
}

function upsertAction(items: RunAction[], patch: RunAction): RunAction[] {
  const id = patch.action_id
  if (!id) return items
  const index = items.findIndex(item => item.action_id === id)
  if (index < 0) return [...items, { status: 'pending', ...patch }]
  const copy = items.slice()
  copy[index] = { ...items[index], ...patch }
  return copy
}

function upsertArtifact(items: RunArtifact[], patch: RunArtifact): RunArtifact[] {
  const id = patch.artifact_id
  if (!id) return items
  const index = items.findIndex(item => item.artifact_id === id)
  if (index < 0) return [...items, patch]
  const copy = items.slice()
  copy[index] = { ...items[index], ...patch }
  return copy
}

function normalizeRefs(value: unknown): ResultRef[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)) return []
    const taskId = text(item.task_id)
    const status = text(item.status)
    if (!taskId || !status) return []
    const ref: ResultRef = { task_id: taskId, status }
    if (typeof item.historical === 'boolean') ref.historical = item.historical
    return [ref]
  })
}

/** 白名单投影：上游动作不能顺带漏出无关字段。 */
function normalizeAction(value: unknown): RunAction | null {
  if (!isRecord(value)) return null
  const action: RunAction = {}
  for (const key of ACTION_FIELDS) {
    if (!(key in value)) continue
    if (key === 'authority_revision') {
      const revision = integer(value[key])
      if (revision !== undefined) action.authority_revision = revision
    } else if (key === 'stale' || key === 'action_confirmed') {
      if (typeof value[key] === 'boolean') action[key] = value[key] as boolean
    } else {
      const field = text(value[key])
      if (field !== undefined) action[key] = field
    }
  }
  return Object.keys(action).length ? action : null
}

function normalizeArtifact(value: unknown): RunArtifact | null {
  if (!isRecord(value)) return null
  const artifact: RunArtifact = {}
  for (const key of ARTIFACT_FIELDS) {
    if (!(key in value)) continue
    if (key === 'size') {
      const size = integer(value[key])
      if (size !== undefined) artifact.size = size
    } else {
      const field = text(value[key])
      if (field !== undefined) artifact[key] = field
    }
  }
  return Object.keys(artifact).length ? artifact : null
}

function normalizeActions(value: unknown): RunAction[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    const action = normalizeAction(item)
    return action ? [action] : []
  })
}

function normalizeArtifacts(value: unknown): RunArtifact[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    const artifact = normalizeArtifact(item)
    return artifact ? [artifact] : []
  })
}

function normalizeError(value: unknown): RunError | null {
  if (!isRecord(value)) return null
  const error: RunError = {}
  if (typeof value.code === 'string') error.code = value.code
  if (typeof value.message === 'string') error.message = value.message
  if (typeof value.retryable === 'boolean') error.retryable = value.retryable
  return Object.keys(error).length ? error : null
}

/** 私有 fetch：运行路由返回裸 JSON，不是旧版信封。 */
export async function runJson(path: string, init: RequestInit = {}): Promise<unknown> {
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (authToken.value) headers.set('Authorization', `Bearer ${authToken.value}`)
  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch (error) {
    if (init.signal?.aborted) throw error
    throw new RunHttpError(0, 'network_unavailable')
  }
  let body: string
  try {
    body = await response.text()
  } catch {
    throw new RunHttpError(response.status, `http_${response.status}`)
  }
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized()
    throw new RunHttpError(response.status, errorCode(body, response.status))
  }
  try {
    return JSON.parse(quoteIntegers(body)) as unknown
  } catch {
    throw new RunProtocolError('invalid_json')
  }
}

async function readSnapshot(id: string, expect: SnapshotExpectation, signal?: AbortSignal): Promise<RunSnapshot> {
  const raw = await runJson(`/api/v1/agent/runs/${encodeURIComponent(id)}`, { signal })
  return normalizeSnapshot(raw, expect)
}

function parseReceipt(raw: unknown): RunReceipt {
  if (!isRecord(raw)) throw new RunProtocolError('invalid_receipt')
  if (raw.protocol !== undefined && raw.protocol !== PROTOCOL) throw new RunProtocolError('protocol_mismatch')
  const runId = text(raw.run_id)
  if (!runId) throw new RunProtocolError('invalid_run_id')
  const sessionId = text(raw.session_id)
  if (!sessionId) throw new RunProtocolError('invalid_session_id')
  const status = asStatus(raw.status)
  if (!status) throw new RunProtocolError('invalid_status')
  return { run_id: runId, turn_id: seqValue(raw.turn_id, 'invalid_turn_id'), session_id: sessionId, status }
}

/**
 * 原始快照的严格投影：不做任何强制类型转换，每个字段都校验，未知字段丢弃，
 * run/turn/session/protocol 绑定与调用方期望比对通过后才允许显示。
 */
function normalizeSnapshot(raw: unknown, expect: SnapshotExpectation = {}): RunSnapshot {
  if (!isRecord(raw)) throw new RunProtocolError('invalid_snapshot')

  const protocol = text(raw.protocol)
  if (!protocol) throw new RunProtocolError('missing_protocol')
  if (protocol !== PROTOCOL || (expect.protocol !== undefined && expect.protocol !== protocol)) {
    throw new RunProtocolError('protocol_mismatch')
  }

  const runId = text(raw.run_id)
  if (!runId) throw new RunProtocolError('invalid_run_id')
  if (expect.run_id !== undefined && expect.run_id !== runId) throw new RunProtocolError('run_binding_mismatch')

  const sessionId = text(raw.session_id)
  if (!sessionId) throw new RunProtocolError('invalid_session_id')
  if (expect.session_id !== undefined && expect.session_id !== sessionId) throw new RunProtocolError('session_binding_mismatch')

  const turnId = seqValue(raw.turn_id, 'invalid_turn_id')
  if (expect.turn_id !== undefined && String(expect.turn_id) !== String(turnId)) {
    throw new RunProtocolError('turn_binding_mismatch')
  }

  const status = asStatus(raw.status)
  if (!status) throw new RunProtocolError('invalid_status')

  return {
    protocol,
    run_id: runId,
    session_id: sessionId,
    turn_id: turnId,
    status,
    authority_sync_status: raw.authority_sync_status === 'pending_reconciliation' ? 'pending_reconciliation' : undefined,
    phase: text(raw.phase) ?? '',
    cancel_requested: raw.cancel_requested === true,
    requires_manual_reconciliation: raw.requires_manual_reconciliation === true,
    final_answer: text(raw.final_answer) ?? '',
    answer_version: raw.answer_version === undefined ? 0 : safeInteger(raw.answer_version, 'invalid_answer_version'),
    result_refs: normalizeRefs(raw.result_refs),
    actions: normalizeActions(raw.actions),
    artifacts: normalizeArtifacts(raw.artifacts),
    terminal_event: normalizeTerminalEvent(raw.terminal_event, { protocol, run_id: runId, turn_id: turnId }),
    last_seq: seqValue(raw.last_seq, 'invalid_last_seq'),
    error: normalizeError(raw.error),
  }
}

function normalizeTerminalEvent(value: unknown, expect: SnapshotExpectation): RunEvent | null {
  if (value === undefined || value === null) return null
  if (!isRecord(value)) throw new RunProtocolError('invalid_terminal_event')
  const type = text(value.type)
  if (!type || !isRunEventType(type)) throw new RunProtocolError('invalid_terminal_event')
  if (value.protocol !== undefined && value.protocol !== expect.protocol) throw new RunProtocolError('protocol_mismatch')
  if (value.run_id !== undefined && value.run_id !== expect.run_id) throw new RunProtocolError('run_binding_mismatch')
  const turnId = value.turn_id === undefined ? expect.turn_id : seqValue(value.turn_id, 'invalid_terminal_event')
  if (expect.turn_id !== undefined && String(turnId) !== String(expect.turn_id)) throw new RunProtocolError('turn_binding_mismatch')
  return {
    protocol: text(value.protocol) ?? expect.protocol ?? '',
    run_id: text(value.run_id) ?? expect.run_id ?? '',
    turn_id: turnId ?? 0,
    seq: seqValue(value.seq, 'invalid_terminal_event'),
    type,
    at: text(value.at) ?? '',
    data: sanitizeData(type, isRecord(value.data) ? value.data : {}),
  }
}

/**
 * 解析原始 JSON 时保住 64 位的 `seq` / `last_seq` / `turn_id` 字面量。
 * 只给 16 位以上的整数加引号，小整数仍是 number。
 */
function quoteIntegers(raw: string): string {
  // 按完整 token 扫描 JSON 字符串，绝不改写已引号内的正文内容。
  let output = '', copied = 0
  for (let i = 0; i < raw.length; i++) {
    if (raw[i] !== '"') continue
    const start = i++
    for (; i < raw.length; i++) { if (raw[i] === '\\') i++; else if (raw[i] === '"') break }
    const end = i + 1
    let key: string
    try { key = JSON.parse(raw.slice(start, end)) as string } catch { continue }
    if (!['last_seq', 'seq', 'turn_id'].includes(key)) continue
    const match = /^(\s*:\s*)(\d{16,})(?=\s*[,}])/.exec(raw.slice(end))
    if (!match) continue
    const numberStart = end + match[1]!.length
    output += raw.slice(copied, numberStart) + '"' + match[2] + '"'
    copied = numberStart + match[2]!.length; i = copied - 1
  }
  return output + raw.slice(copied)
}

function eventsUrl(id: string, cursor: Seq): string {
  const params = new URLSearchParams({ after_seq: seqText(cursor) })
  return `/api/v1/agent/runs/${encodeURIComponent(id)}/events?${params.toString()}`
}

function seqText(value: Seq): string {
  return typeof value === 'number' ? String(Math.trunc(value)) : value
}

/** 精确 64 位值：能安全表示就是 number，否则保留原始字面量。 */
function seqValue(value: unknown, code: string): Seq {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || value < 0) throw new RunProtocolError(code)
    return value
  }
  if (typeof value === 'string' && /^\d+$/.test(value) && BigInt(value) <= 9223372036854775807n) {
    const parsed = Number(value)
    if (Number.isSafeInteger(parsed) && String(parsed) === value) return parsed
    return value
  }
  throw new RunProtocolError(code)
}

function safeInteger(value: unknown, code: string): number {
  const parsed = seqValue(value, code)
  if (typeof parsed === 'number') return parsed
  throw new RunProtocolError(code)
}

function toBigInt(value: Seq): bigint {
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) throw new RunProtocolError('invalid_seq')
    return BigInt(value)
  }
  if (!/^-?\d+$/.test(value)) throw new RunProtocolError('invalid_seq')
  return BigInt(value)
}

function sameInteger(left: string, right: string): boolean {
  try { return BigInt(left) === BigInt(right) } catch { return false }
}

function isRunEventType(value: string): value is RunEventType {
  return (EVENT_TYPES as readonly string[]).includes(value)
}

function asStatus(value: unknown): RunStatus | undefined {
  return typeof value === 'string' && (
    value === 'queued' || value === 'running' || value === 'awaiting_approval' || value === 'reconciling'
    || value === 'succeeded' || value === 'partial' || value === 'failed' || value === 'cancelled'
    || value === 'pending_reconciliation'
  ) ? value : undefined
}

export function isTerminalStatus(status: RunStatus): boolean {
  return status === 'succeeded' || status === 'partial' || status === 'failed' || status === 'cancelled'
}

function isTerminalEvent(type: RunEventType): boolean {
  return type === 'completed' || type === 'failed' || type === 'cancelled'
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function integer(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) ? value : undefined
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function backoffDelay(attempt: number): number {
  return Math.min(MAX_BACKOFF_MS, 500 * 2 ** Math.max(0, attempt - 1))
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    if (signal?.aborted) {
      resolve()
      return
    }
    let timer: ReturnType<typeof setTimeout>
    const done = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', done)
      resolve()
    }
    timer = setTimeout(done, ms)
    signal?.addEventListener('abort', done, { once: true })
  })
}

/** 错误体可能是网关信封，也可能是上游原始错误对象。 */
function errorCode(body: string, status: number): string {
  try {
    const parsed = JSON.parse(body) as unknown
    if (isRecord(parsed)) {
      const code = text(parsed.code) || text(parsed.detail)
      if (code) return code
      const message = text(parsed.message)
      if (message) return message
      if (typeof parsed.code === 'number') return String(parsed.code)
    }
  } catch { /* 非 JSON 错误体 */ }
  return `http_${status}`
}

async function errorCodeFromResponse(response: Response): Promise<string> {
  try {
    return errorCode(await response.text(), response.status)
  } catch {
    return `http_${response.status}`
  }
}

async function finalSnapshot(id: string, expect: SnapshotExpectation, signal?: AbortSignal): Promise<RunSnapshot | null> {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      return await readSnapshot(id, expect, signal)
    } catch (error) {
      if (signal?.aborted) return null
      const status = error instanceof RunHttpError ? error.status : undefined
      if (status === 401 || status === 403 || status === 404) throw error
      if (attempt === 3) throw new RunProtocolError('final_snapshot_unavailable')
      await wait(backoffDelay(attempt), signal)
    }
  }
  return null
}
