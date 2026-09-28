import { clearSession, token } from './auth'
import type { Alarm, ApiEnvelope, Device, History, LoginResult, Metric, Page, Prediction, PredictionListItem, Summary, Telemetry } from '../types'

export class ApiError extends Error {
  constructor(public status: number, public code: number, message: string) { super(message) }
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
  catch { throw new ApiError(0, 0, '无法连接后端服务，请检查本机服务状态') }
  let envelope: ApiEnvelope<T>
  try { envelope = await response.json() as ApiEnvelope<T> }
  catch { throw new ApiError(response.status, 0, response.ok ? '服务返回了无法解析的数据' : `后端服务暂不可用（HTTP ${response.status}）`) }
  if (!response.ok || envelope.code !== 0) {
    if (response.status === 401 && token.value) {
      clearSession()
      window.location.replace('/login')
    }
    throw new ApiError(response.status, envelope.code, envelope.message || '请求失败')
  }
  return envelope.data
}

export const api = {
  login: (username: string, password: string) => request<LoginResult>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  devices: (page = 1, pageSize = 20, groupName = '') => request<Page<Device>>(`/api/v1/devices${query({ page, page_size: pageSize, group_name: groupName })}`),
  device: (id: number) => request<Device>(`/api/v1/devices/${id}`),
  latest: (id: number) => request<Telemetry>(`/api/v1/devices/${id}/telemetry/latest`),
  history: (id: number, metric: Metric, start: Date, end: Date) => request<History>(`/api/v1/devices/${id}/telemetry${query({ metric, start: start.toISOString(), end: end.toISOString() })}`),
  summary: () => request<Summary>('/api/v1/dashboard/summary'),
  alarms: (page = 1, pageSize = 20, status = '', level = '') => request<Page<Alarm>>(`/api/v1/alarms${query({ page, page_size: pageSize, status, level })}`),
  alarm: (id: number) => request<Alarm>(`/api/v1/alarms/${id}`),
  ackAlarm: (id: number) => request<Alarm>(`/api/v1/alarms/${id}/ack`, { method: 'POST' }),
  predictions: (page = 1, pageSize = 20) => request<Page<PredictionListItem>>(`/api/v1/predictions${query({ page, page_size: pageSize })}`),
  prediction: (id: number) => request<Prediction>(`/api/v1/devices/${id}/prediction`),
  ticket: () => request<{ ticket: string; expires_in: number }>('/api/v1/ws-ticket', { method: 'POST' }),
}
