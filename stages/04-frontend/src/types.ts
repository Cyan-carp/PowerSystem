export interface ApiEnvelope<T> { code: number; message: string; data: T }
export interface Page<T> { list: T[]; page: number; page_size: number; total: number }
export interface User { id: number; username: string; real_name: string; role: string }
export interface LoginResult { token: string; token_type: string; expires_in: number; user: User }
export interface Device {
  id: number; device_code: string; name: string; dev_type: string; vendor: string
  station_code: string; group_name: string; created_at: string; updated_at: string
}
export interface Telemetry {
  device_id: number; device_code: string; run_id: string; seq: number; ts_ms: number
  received_at: string; voltage: number; current: number; temperature: number
  power: number; status: number; fault_code: number
}
export type Metric = 'voltage' | 'current' | 'temperature' | 'power'
export interface History { device_id: number; metric: Metric; points: [number | string, number][] }
export interface Summary {
  device_total: number; online: number; offline: number; fault: number
  current_power_kw: number; active_alarms: number; operational_health_percent: number
  today_energy_kwh: number | null; retained_energy_kwh: number | null
  energy_start_ms: number | null; energy_updated_at: string | null
  health_score_percent: number | null
}
export type AlarmLevel = 'urgent' | 'major' | 'minor'
export type AlarmStatus = 'unhandled' | 'acked' | 'recovered'
export interface Alarm {
  id: number; device_id: number; rule_id: number | null; metric: string; level: AlarmLevel
  value: number; threshold: number; status: AlarmStatus; acked_by: number | null
  acked_at: string | null; recovered_at: string | null; triggered_at: string
}
export interface PredictionListItem {
  device_id: number; device_code: string; name: string; station_code: string; group_name: string
  window_end_ms: number | null; probability: number | null; threshold: number | null
  risk_level: 'high' | 'low' | null; model_version: string | null; source: string | null
  stale: boolean
}
export interface Factor { feature: string; value: number; contribution: number }
export interface Prediction {
  device_id: number; window_end_ms: number; probability: number; threshold: number
  risk_level: 'high' | 'low'; model_version: string; top_factors: Factor[]
  source: string; created_at: string; stale: boolean
}
export type RealtimeEvent =
  | { type: 'interpretation_updated'; data: { id?: number; status?: string; updated_at: string } }
  | { type: 'agent_model_updated'; data: { available: boolean } }
  | { type: 'connected' | 'disconnected' }
  | { type: 'telemetry'; data: Telemetry }
  | { type: 'alarm_created' | 'alarm_acked' | 'alarm_recovered'; data: Alarm }
  | { type: 'prediction'; data: { device_id: number } }

export interface AgentEvidence {
  id: string; tool: string; status: string; source: string; collected_at: string; data_time: string | null
  data: Record<string, unknown>
}
export interface AgentClaim { text: string; evidence_ids: string[]; equipment_operation?: boolean }
export interface Interpretation {
  id: number; event_key: string; category: 'device_alarm' | 'prediction_risk' | 'platform_monitor'
  alarm_id: number | null; level: AlarmLevel; occurred_at: string; updated_at: string
  task_status: 'pending' | 'running' | 'completed' | 'degraded'; reason: string; active: boolean; read: boolean
  alarm: Alarm | null; monitor: Record<string, unknown> | null; evidence: AgentEvidence[] | null
  result: { status: string; conclusion: AgentClaim | null; suggestions: AgentClaim[]; limitations: string[]; model?: string } | null
}
export interface AgentModelStatus { available: boolean; reason: string; message: string }

export interface ChatEvidence extends AgentEvidence {
  kind: 'business' | 'document' | 'web'; document_version?: string; chapter?: string; url?: string
}
export interface AgentChatResponse {
  request_id: string; session_id: string; status: 'answered' | 'unable_to_determine' | 'degraded'
  conclusion: AgentClaim | null; suggestions: AgentClaim[]; evidence: ChatEvidence[]
  limitations: string[]; notices: string[]; model: string; knowledge_status: string; web_status: string; miss_record_status: string
}
export interface KnowledgeSource { id: string; document: string; heading: string; content: string; version: string; authority: string; document_date?: string }
