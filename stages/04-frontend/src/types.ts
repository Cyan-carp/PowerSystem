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
  | { type: 'connected' | 'disconnected' }
  | { type: 'telemetry'; data: Telemetry }
  | { type: 'alarm_created' | 'alarm_acked' | 'alarm_recovered'; data: Alarm }
  | { type: 'prediction'; data: { device_id: number } }
