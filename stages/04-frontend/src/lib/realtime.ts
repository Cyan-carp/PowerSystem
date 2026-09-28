import { reactive, ref } from 'vue'
import { api } from './api'
import { token } from './auth'
import type { RealtimeEvent, Telemetry } from '../types'

type Listener = (event: RealtimeEvent) => void
const listeners = new Set<Listener>()
const latest = reactive<Record<number, Telemetry>>({})
const connected = ref(false)
const reconnectAttempt = ref(0)
let socket: WebSocket | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let active = false
let generation = 0

function emit(event: RealtimeEvent): void {
  for (const listener of listeners) listener(event)
}

function retry(epoch: number): void {
  if (!active || epoch !== generation) return
  const delay = Math.min(30000, 1000 * 2 ** Math.min(reconnectAttempt.value, 5))
  reconnectAttempt.value++
  retryTimer = setTimeout(() => { void connect(epoch) }, delay)
}

async function connect(epoch: number): Promise<void> {
  if (!active || epoch !== generation || !token.value) return
  try {
    const { ticket } = await api.ticket()
    if (!active || epoch !== generation) return
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${scheme}//${location.host}/ws/realtime?ticket=${encodeURIComponent(ticket)}`)
    socket = ws
    ws.onopen = () => {
      if (epoch !== generation) return
      connected.value = true
      reconnectAttempt.value = 0
      emit({ type: 'connected' })
    }
    ws.onmessage = (message) => {
      try {
        const event = JSON.parse(String(message.data)) as RealtimeEvent
        if (event.type === 'telemetry') latest[event.data.device_id] = event.data
        emit(event)
      } catch { /* ignore malformed external event */ }
    }
    ws.onclose = () => {
      if (epoch !== generation) return
      socket = null
      connected.value = false
      emit({ type: 'disconnected' })
      retry(epoch)
    }
    ws.onerror = () => ws.close()
  } catch {
    connected.value = false
    emit({ type: 'disconnected' })
    retry(epoch)
  }
}

export const realtime = {
  connected,
  reconnectAttempt,
  latest,
  start(): void {
    if (active) return
    active = true
    generation++
    void connect(generation)
  },
  stop(): void {
    active = false
    generation++
    if (retryTimer) clearTimeout(retryTimer)
    retryTimer = null
    socket?.close()
    socket = null
    connected.value = false
  },
  subscribe(listener: Listener): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
}
