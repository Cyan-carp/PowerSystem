import { runJson, type RunReceipt } from './agent-runs'
export interface ScheduleConfig {
  task_type: 'daily_report' | 'inspection'; scope: 'all' | 'business' | 'baseline'
  station_code: string | null; device_id: number | null; timezone: string
  mode: 'daily' | 'interval'; daily_time: string | null; interval_minutes: number | null
  grant_days: number; destination_id: 'platform_client'; destination_version: '1'
}
export interface Schedule {
  id: string; config_version: number; config: ScheduleConfig; status: 'active' | 'paused' | 'cancelled'
  next_run_at: string | null; reason: string | null
  grant: { grant_id: string; expires_at: string; revoked_at: string | null } | null
}
export interface HistoryItem { scheduled_for: string; run_id: string | null; run_status: string | null; status: string; reason: string | null; skipped_count: number }
export const defaultConfig = (): ScheduleConfig => ({ task_type: 'daily_report', scope: 'business', station_code: null, device_id: null, timezone: 'Asia/Shanghai', mode: 'daily', daily_time: '09:00', interval_minutes: null, grant_days: 30, destination_id: 'platform_client', destination_version: '1' })
const root = '/api/v1/agent/schedules'
export async function listSchedules() { return await runJson(root) as { list: Schedule[]; runs: (RunReceipt & { capability: string })[] } }
export async function scheduleHistory(id: string, offset = 0) { return await runJson(`${root}/${encodeURIComponent(id)}/history?offset=${offset}`) as { list: HistoryItem[] } }
export async function proposeSchedule(config: ScheduleConfig, selected?: Schedule, key = crypto.randomUUID()) {
  return await runJson(selected ? `${root}/${encodeURIComponent(selected.id)}` : root, { method: selected ? 'PUT' : 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(selected ? { config, expected_version: selected.config_version } : { config }) }) as RunReceipt
}
export async function controlSchedule(item: Schedule, action: 'pause' | 'cancel' | 'resume') {
  return await runJson(`${root}/${encodeURIComponent(item.id)}/${action}`, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(action === 'resume' ? { expected_version: item.config_version } : {}) }) as Schedule | RunReceipt
}
export async function previewRule(body: Record<string, unknown>, key = crypto.randomUUID()) {
  return await runJson('/api/v1/agent/rule-preview', { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(body) }) as RunReceipt
}
