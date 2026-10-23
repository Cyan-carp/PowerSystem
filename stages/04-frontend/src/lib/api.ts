import { clearSession, token } from './auth'
import { retryAfterSeconds } from './retry-after'
import type { OpsStatus, OpsTask } from '../types'
import type { Alarm, ApiEnvelope, Device, History, LoginResult, Metric, Overview, Page, Prediction, PredictionListItem, Scope, Station, Summary, Telemetry, Interpretation, AgentModelStatus, KnowledgeSource, RAGChatResponse, RAGSession, RAGTurn, VerificationInput, DeviceHealth, StationHealth, AvailabilityItem, StationAvailability, TimelinessResult } from '../types'

export class ApiError extends Error {
  constructor(public status: number, public code: number, message: string, public retryAfter = 0) { super(message) }
}

export interface ScopeQuery { scope?: Scope; stationCode?: string }

function scopeParams(filter: ScopeQuery = {}): { scope?: Scope; station_code?: string } {
  return { scope: filter.scope, station_code: filter.stationCode }
}

function query(params: Record<string, string | number | undefined>): string {
  const values = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') values.set(key, String(value))
  }
  const result = values.toString()
  return result ? `?${result}` : ''
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body) headers.set('Content-Type', 'application/json')
  if (token.value) headers.set('Authorization', `Bearer ${token.value}`)
  let response: Response
  try { response = await fetch(path, { ...init, headers }) }
  catch (error) {
    if (init.signal?.aborted) throw error
    throw new ApiError(0, 0, '无法连接后端服务，请检查本机服务状态')
  }
  let envelope: ApiEnvelope<T>
  const retryAfter = retryAfterSeconds(response.headers?.get('Retry-After'))
  try { envelope = await response.json() as ApiEnvelope<T> }
  catch { throw new ApiError(response.status, 0, response.ok ? '服务返回了无法解析的数据' : `后端服务暂不可用（HTTP ${response.status}）`, retryAfter) }
  if (!response.ok || envelope.code !== 0) {
    if (response.status === 401 && token.value) {
      clearSession()
      window.location.replace('/login')
    }
    throw new ApiError(response.status, envelope.code, envelope.message || '请求失败', retryAfter)
  }
  return envelope.data
}

export async function downloadFile(path: string, filename: string): Promise<void> {
  const headers = new Headers()
  if (token.value) headers.set('Authorization', `Bearer ${token.value}`)
  let response: Response
  try { response = await fetch(path, { headers }) }
  catch { throw new ApiError(0, 0, '无法连接后端服务，请检查本机服务状态') }
  if (!response.ok) {
    let message = `后端服务暂不可用（HTTP ${response.status}）`
    try {
      const envelope = await response.json() as ApiEnvelope<unknown>
      message = envelope.message || message
    } catch { /* ignore */ }
    if (response.status === 401 && token.value) {
      clearSession()
      window.location.replace('/login')
    }
    throw new ApiError(response.status, 0, message)
  }
  if (!response.headers.get('Content-Type')?.toLowerCase().includes('text/csv')) throw new ApiError(response.status, 0, '下载未返回 CSV 文件，请重试')
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

export const api = {
  opsTasks: (sessionID?: string, offset = 0, options: { status?: OpsStatus; limit?: number; signal?: AbortSignal } = {}) => request<{ list: OpsTask[]; offset: number; page_size: number }>(`/api/v1/agent/tasks${query({ session_id: sessionID, offset, status: options.status, limit: options.limit })}`, { signal: options.signal }),
  opsTask: (id: string, signal?: AbortSignal) => request<OpsTask>(`/api/v1/agent/tasks/${encodeURIComponent(id)}`, { signal }),
  cancelOpsTask: (id: string) => request<OpsTask>(`/api/v1/agent/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  deleteOpsTask: (id: string) => request<{ task_id: string; deleted: boolean; already_deleted: boolean; deleted_at: string }>(`/api/v1/agent/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  decideOpsApproval: (id: string, approvalID: string, decision: 'approve' | 'reject') => request<OpsTask>(`/api/v1/agent/tasks/${encodeURIComponent(id)}/approvals/${encodeURIComponent(approvalID)}/decision`, { method: 'POST', body: JSON.stringify({ decision }) }),
  chat: (message: string, sessionID?: string, signal?: AbortSignal) => request<RAGChatResponse>('/api/v1/agent/chat', {
    method: 'POST', body: JSON.stringify({ message, ...(sessionID ? { session_id: sessionID } : {}) }), signal,
  }),
  chatSessions: (offset = 0) => request<{ list: RAGSession[]; offset: number; page_size: number }>(`/api/v1/agent/sessions?offset=${offset}`),
  verifyChat: (id: string, body: VerificationInput, signal?: AbortSignal) => request<RAGChatResponse>(`/api/v1/agent/sessions/${encodeURIComponent(id)}/verify`, { method: 'POST', body: JSON.stringify(body), signal }),
  chatSession: (id: string) => request<{ session_id: string; turns: RAGTurn[] }>(`/api/v1/agent/sessions/${encodeURIComponent(id)}`),
  deleteChatSession: (id: string) => request(`/api/v1/agent/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  ragSource: (id: string) => request<{ document: string; heading: string; content: string; version: string; page?: number }>(`/api/v1/agent/rag/sources/${encodeURIComponent(id)}`),
  knowledge: (id: string, signal?: AbortSignal) => request<KnowledgeSource>(`/api/v1/agent/knowledge/${encodeURIComponent(id)}`, { signal }),
  interpretations: (page = 1, attention = false) => request<Page<Interpretation>>(`/api/v1/agent/interpretations${query({ page, page_size: 100, attention: attention ? 'true' : undefined })}`),
  interpretation: (id: number) => request<Interpretation>(`/api/v1/agent/interpretations/${id}`),
  readInterpretation: (id: number) => request(`/api/v1/agent/interpretations/${id}/read`, { method: 'POST' }),
  modelStatus: () => request<AgentModelStatus>('/api/v1/agent/model-status'),
  modelProbe: () => request('/api/v1/agent/model-probe', { method: 'POST' }),
  login: (username: string, password: string) => request<LoginResult>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  devices: (page = 1, pageSize = 20, groupName = '', filter: ScopeQuery = {}) => request<Page<Device>>(`/api/v1/devices${query({ page, page_size: pageSize, group_name: groupName, ...scopeParams(filter) })}`),
  stations: (scope: Scope = 'all') => request<{ scope: Scope; list: Station[]; total: number }>(`/api/v1/stations${query({ scope })}`),
  overview: (scope: Scope = 'all', stationCode = '') => request<Overview>(`/api/v1/dashboard/overview${query({ scope, station_code: stationCode })}`),
  device: (id: number) => request<Device>(`/api/v1/devices/${id}`),
  latest: (id: number) => request<Telemetry>(`/api/v1/devices/${id}/telemetry/latest`),
  history: (id: number, metric: Metric, start: Date, end: Date) => request<History>(`/api/v1/devices/${id}/telemetry${query({ metric, start: start.toISOString(), end: end.toISOString() })}`),
  summary: () => request<Summary>('/api/v1/dashboard/summary'),
  alarms: (page = 1, pageSize = 20, status = '', level = '', filter: ScopeQuery & { origin?: 'normal' | 'simulation' } = {}) => request<Page<Alarm>>(`/api/v1/alarms${query({ page, page_size: pageSize, status, level, origin: filter.origin, ...scopeParams(filter) })}`),
  alarm: (id: number) => request<Alarm>(`/api/v1/alarms/${id}`),
  ackAlarm: (id: number) => request<Alarm>(`/api/v1/alarms/${id}/ack`, { method: 'POST' }),
  archiveAlarm: (id: number) => request<Alarm>(`/api/v1/alarms/${id}/archive`, { method: 'POST' }),
  archiveAlarms: (ids: number[]) => request<{ archived: number; failed: number[] }>('/api/v1/alarm-archive', { method: 'POST', body: JSON.stringify({ ids }) }),
  predictions: (page = 1, pageSize = 20) => request<Page<PredictionListItem>>(`/api/v1/predictions${query({ page, page_size: pageSize })}`),
  prediction: (id: number) => request<Prediction>(`/api/v1/devices/${id}/prediction`),
  healthDevices: () => request<{ rule_version: string; computed_at: string; devices: DeviceHealth[]; stations: StationHealth[]; complete: boolean }>('/api/v1/health/devices'),
  healthStations: () => request<{ rule_version: string; computed_at: string; stations: StationHealth[] }>('/api/v1/health/stations'),
  availability: (params: { start?: string; end?: string; device_id?: number; station_code?: string } = {}, signal?: AbortSignal) => request<{ rule_version: string; computed_at: string; start: string; end: string; devices: AvailabilityItem[]; stations: StationAvailability[] }>(`/api/v1/reports/availability${query(params)}`, { signal }),
  alarmTimeliness: (params: { start?: string; end?: string; device_id?: number; station_code?: string } = {}, signal?: AbortSignal) => request<{ rule_version: string; computed_at: string; start: string; end: string; scope: string; result: TimelinessResult }>(`/api/v1/reports/alarm-timeliness${query(params)}`, { signal }),
  exportTelemetry: (id: number, start: Date, end: Date) => downloadFile(`/api/v1/devices/${id}/telemetry/export${query({ start: start.toISOString(), end: end.toISOString() })}`, `device-${id}-telemetry.csv`),
  exportReport: (kind: 'availability' | 'alarm-timeliness', params: {start: string; end: string; device_id?: number; station_code?: string}) => downloadFile(`/api/v1/reports/${kind}${query({...params, format: 'csv'})}`, `${kind}.csv`),
  ticket: () => request<{ ticket: string; expires_in: number }>('/api/v1/ws-ticket', { method: 'POST' }),
}
