import { reactive } from 'vue'
import { api } from './api'
import type { Telemetry } from './api'
import { receiveAlarmNotice } from './alarm-notices'
import { initDesktop, resetDesktopSession } from './desktop'

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
let running = false
let generation = 0

/* 真实后端要求先取一次性 ticket（POST /ws-ticket），再用 /ws/realtime?ticket= 建链 */
const open = async () => {
  if (!running || socket || connecting) return
  const mine = generation
  connecting = true
  try {
    const { ticket } = await api.ticket()
    if (!running || mine !== generation) return
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${scheme}://${location.host}/ws/realtime?ticket=${encodeURIComponent(ticket)}`)
    socket = ws
    ws.onopen = () => {
      if (!running || mine !== generation) return
      shell.live = true
      // A short reconnect may have missed the live event. Only fresh pending
      // alarms are eligible; the same session deduplicates them with WS frames.
      void api.alarms({ status: 'unhandled', pageSize: 20, start: new Date(Date.now() - 120_000).toISOString() }).then(page => {
        if (!running || mine !== generation || socket !== ws) return
        for (const alarm of [...page.list].reverse()) receiveAlarmNotice('alarm_created', alarm)
      }).catch(() => { /* the live stream remains usable when catch-up fails */ })
    }
    ws.onmessage = (event) => {
      if (!running || mine !== generation || socket !== ws) return
      try {
        const payload = JSON.parse(String(event.data)) as { type: string; data: Telemetry }
        if (payload.type === 'telemetry') {
          shell.pulse = payload.data
          shell.pulseAt = Date.now()
        } else if (ALARM_EVENTS.has(payload.type)) {
          shell.alarmRevision += 1
          receiveAlarmNotice(payload.type, payload.data)
        } else if (payload.type === 'interpretation_updated') {
          /* 后端 worker 每次改 task_status 都推这一帧；面板据此提前结束等待，不用干等轮询 */
          shell.insightRevision += 1
        }
      } catch {
        /* 帧格式异常不影响界面 */
      }
    }
    ws.onclose = () => {
      if (!running || mine !== generation || socket !== ws) return
      shell.live = false
      socket = null
      retryTimer = window.setTimeout(open, 5000)
    }
    ws.onerror = () => ws.close()
  } catch {
    if (!running || mine !== generation) return
    shell.live = false
    retryTimer = window.setTimeout(open, 8000)
  } finally {
    if (mine === generation) connecting = false
  }
}

export function connectRealtime() {
  if (running) return
  running = true
  void initDesktop()
  void open()
}

export function closeRealtime() {
  running = false
  generation += 1
  connecting = false
  if (retryTimer) window.clearTimeout(retryTimer)
  retryTimer = undefined
  socket?.close()
  socket = null
  shell.live = false
  shell.pulse = null
  resetDesktopSession()
}

setInterval(() => {
  shell.now = new Date()
}, 1000)
