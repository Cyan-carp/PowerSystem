import { reactive } from 'vue'
import type { Alarm, AlarmLevel } from './api'
import { notifyDesktopAlarm, onSessionReset } from './desktop'

export const alarmNotices = reactive<Alarm[]>([])
const seen = new Set<number>()
const levels: AlarmLevel[] = ['urgent', 'major', 'minor']

export function dismissAlarmNotice(id: number): void {
  const index = alarmNotices.findIndex(alarm => alarm.id === id)
  if (index >= 0) alarmNotices.splice(index, 1)
}

export function resetAlarmNotices(): void {
  alarmNotices.splice(0)
  seen.clear()
}
onSessionReset(resetAlarmNotices)

// Only new, fresh trigger events prompt the operator. Acknowledgements,
// recoveries, reconnect duplicates and historical replays only refresh pages.
export function receiveAlarmNotice(type: string, value: unknown): void {
  if (type !== 'alarm_created' && type !== 'alarm_triggered') return
  if (!value || typeof value !== 'object') return
  const alarm = value as Alarm
  const age = Date.now() - Date.parse(alarm.triggered_at)
  if (!Number.isSafeInteger(alarm.id) || alarm.id <= 0 || !Number.isSafeInteger(alarm.device_id) || alarm.device_id <= 0 ||
      !levels.includes(alarm.level) || alarm.status !== 'unhandled' || typeof alarm.metric !== 'string' ||
      !Number.isFinite(alarm.value) || !Number.isFinite(alarm.threshold) || !Number.isFinite(age) || age < -30_000 || age > 120_000 ||
      seen.has(alarm.id) || seen.size >= 10_000) return
  seen.add(alarm.id)
  alarmNotices.unshift(alarm)
  alarmNotices.splice(5)
  void notifyDesktopAlarm(alarm.id, alarm.level)
}
