<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElNotification } from 'element-plus'
import { Bell, Calendar, ChatDotSquare, DataAnalysis, DataBoard, Monitor, TrendCharts, Files } from '@element-plus/icons-vue'
import { clearSession, currentUser } from '../lib/auth'
import { realtime } from '../lib/realtime'
import { api } from '../lib/api'
import { dateTime, levelLabels } from '../lib/format'
import PageHelp from '../components/PageHelp.vue'
import InterpretationPanel from '../components/InterpretationPanel.vue'

const route = useRoute()
const router = useRouter()
const now = ref(Date.now())
const soundOn = ref(sessionStorage.getItem('powersystem_sound') !== 'off')
const activeAlarms = ref(0)
let clock: ReturnType<typeof setInterval> | null = null
let unsubscribe: (() => void) | null = null

const navItems = [
  { to: '/dashboard', match: (path: string) => path === '/dashboard', icon: DataBoard, label: '总览大屏' },
  { to: '/devices', match: (path: string) => path.startsWith('/devices'), icon: Monitor, label: '设备管理' },
  { to: '/health', match: (path: string) => path === '/health', icon: TrendCharts, label: '健康排行' },
  { to: '/reports', match: (path: string) => path === '/reports', icon: Files, label: '统计报表' },
  { to: '/alarms', match: (path: string) => path === '/alarms', icon: Bell, label: '告警中心' },
  { to: '/predictions', match: (path: string) => path === '/predictions', icon: DataAnalysis, label: '故障预测' },
  { to: '/agent', match: (path: string) => path === '/agent', icon: ChatDotSquare, label: '智能助手' },
  { to: '/agent/schedules', match: (path: string) => path.startsWith('/agent/schedules'), icon: Calendar, label: '定期任务' },
]

const userName = computed(() => currentUser.value?.real_name || currentUser.value?.username || '值班员')

function toggleSound(): void {
  soundOn.value = !soundOn.value
  sessionStorage.setItem('powersystem_sound', soundOn.value ? 'on' : 'off')
}

function beep(): void {
  if (!soundOn.value) return
  try {
    const context = new AudioContext()
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    oscillator.type = 'sine'
    oscillator.frequency.value = 720
    gain.gain.setValueAtTime(0.0001, context.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.12, context.currentTime + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.22)
    oscillator.connect(gain).connect(context.destination)
    oscillator.start()
    oscillator.stop(context.currentTime + 0.23)
    oscillator.onended = () => { void context.close() }
  } catch { /* browser audio permission does not affect the visual alert */ }
}

function logout(): void {
  realtime.stop()
  clearSession()
  void router.replace('/login')
}

async function refreshAlarmBadge(): Promise<void> {
  try {
    const result = await api.alarms(1, 1, 'unhandled')
    activeAlarms.value = result.total
  } catch { /* badge stays stale; visual only */ }
}

onMounted(() => {
  clock = setInterval(() => { now.value = Date.now() }, 1000)
  void refreshAlarmBadge()
  unsubscribe = realtime.subscribe((event) => {
    if (event.type === 'alarm_created') {
      beep()
      void refreshAlarmBadge()
      ElNotification({
        title: `${levelLabels[event.data.level]}告警 · 设备 #${event.data.device_id}`,
        message: event.data.metric === 'ai_failure_risk' ? '预测风险升高，请核查设备状态' : `${event.data.metric} 越限，请及时核查`,
        type: event.data.level === 'urgent' ? 'error' : 'warning',
        duration: 9000,
        onClick: () => { void router.push({ path: '/alarms', query: { id: String(event.data.id) } }) },
      })
    }
    if (event.type === 'alarm_acked' || event.type === 'alarm_recovered') void refreshAlarmBadge()
  })
  realtime.start()
})
onUnmounted(() => {
  if (clock) clearInterval(clock)
  unsubscribe?.()
  realtime.stop()
})
</script>

<template>
  <div class="app-shell">
    <nav class="sidebar" aria-label="主导航">
      <router-link class="brand-mark" to="/dashboard" aria-label="返回运行总览">
        <strong>源网智联</strong>
        <small>新能源设备智能运维</small>
      </router-link>
      <div class="nav-list">
        <router-link
          v-for="item in navItems" :key="item.to"
          class="nav-item" :class="{ active: item.match(route.path) }"
          :to="item.to" :title="item.label" :aria-label="item.label" :aria-current="item.match(route.path) ? 'page' : undefined"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
          <span v-if="item.to === '/alarms' && activeAlarms" class="nav-badge">{{ activeAlarms > 99 ? '99+' : activeAlarms }}</span>
        </router-link>
      </div>
      <div class="sidebar-foot">
        <span class="foot-user" :title="userName">{{ userName }}</span>
        <button class="foot-logout" type="button" @click="logout">退出登录</button>
      </div>
    </nav>
    <div class="app-col">
      <header class="topbar">
        <strong class="topbar-title">{{ route.meta.title || '运行工作台' }}</strong>
        <div class="topbar-actions">
          <PageHelp />
          <InterpretationPanel />
          <span class="connection" :class="realtime.connected.value ? 'online' : 'offline'"><span class="signal-dot" />{{ realtime.connected.value ? '实时连接正常' : '实时连接中断' }}</span>
          <span class="clock">{{ dateTime(now) }}</span>
          <button class="sound-btn" type="button" @click="toggleSound">告警提示音：{{ soundOn ? '开' : '关' }}</button>
        </div>
      </header>
      <main class="main-content"><router-view /></main>
    </div>
  </div>
</template>
