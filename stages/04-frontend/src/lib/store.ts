import { reactive } from 'vue'
import { api } from './api'
import type { Telemetry } from './api'

/** 跨页共享的最小状态：只在壳层需要的地方保留，其余各页自取。 */
export const shell = reactive({
  alarmsOpen: 0,
  onlineDevices: 0,
  deviceTotal: 0,
  now: new Date(),
  live: false,
  pulse: null as Telemetry | null,
  pulseAt: 0,
  /* 写接口广播的告警变更计数：别的标签页确认/归档后，本页据此重新取数 */
  alarmRevision: 0,
  /* 解读任务进度变更计数（pending→completed/degraded）：解读面板据此停止「生成中」 */
  insightRevision: 0,
})

/** 后端在写接口成功后广播的事件类型（真后端 broadcastAfterWrite，mock 同契约）。 */
const ALARM_EVENTS = new Set(['alarm_acked', 'alarm_archived', 'alarm_triggered', 'alarm_recovered', 'alarm_created'])

let socket: WebSocket | null = null
let connecting = false
let retryTimer: number | undefined

/* 真实后端要求先取一次性 ticket（POST /ws-ticket），再用 /ws/realtime?ticket= 建链 */
const open = async () => {
  if (socket || connecting) return
  connecting = true
  try {
    const { ticket } = await api.ticket()
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${scheme}://${location.host}/ws/realtime?ticket=${encodeURIComponent(ticket)}`)
    socket = ws
    ws.onopen = () => {
      shell.live = true
    }
    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(String(event.data)) as { type: string; data: Telemetry }
        if (payload.type === 'telemetry') {
          shell.pulse = payload.data
          shell.pulseAt = Date.now()
        } else if (ALARM_EVENTS.has(payload.type)) {
          shell.alarmRevision += 1
        } else if (payload.type === 'interpretation_updated') {
          /* 后端 worker 每次改 task_status 都推这一帧；面板据此提前结束等待，不用干等轮询 */
          shell.insightRevision += 1
        }
      } catch {
        /* 帧格式异常不影响界面 */
      }
    }
    ws.onclose = () => {
      shell.live = false
      socket = null
      retryTimer = window.setTimeout(open, 5000)
    }
    ws.onerror = () => ws.close()
  } catch {
    shell.live = false
    retryTimer = window.setTimeout(open, 8000)
  } finally {
    connecting = false
  }
}

export function connectRealtime() {
  void open()
}

export function closeRealtime() {
  if (retryTimer) window.clearTimeout(retryTimer)
  socket?.close()
  socket = null
}

setInterval(() => {
  shell.now = new Date()
}, 1000)
