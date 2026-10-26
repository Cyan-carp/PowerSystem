<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, type Component } from 'vue'
import { useRoute, useRouter, type RouteLocationRaw } from 'vue-router'
import { ElNotification } from 'element-plus'
import { Aim, BellFilled, Bell, Calendar, ChatDotSquare, Connection, DataAnalysis, DataBoard, Monitor, MuteNotification, QuestionFilled, SwitchButton, TrendCharts, Files, Warning } from '@element-plus/icons-vue'
import { clearSession, currentUser } from '../lib/auth'
import { realtime } from '../lib/realtime'
import { api } from '../lib/api'
import { dateTime, levelLabels } from '../lib/format'
import type { Summary } from '../types'
import PageHelp from '../components/PageHelp.vue'
import InterpretationPanel from '../components/InterpretationPanel.vue'
import { activeInspector, closeInspector } from '../lib/inspector'

const route = useRoute()
const router = useRouter()
const now = ref(Date.now())
const soundOn = ref(sessionStorage.getItem('powersystem_sound') !== 'off')
const activeAlarms = ref(0)
const urgentAlarms = ref(0)
const summary = ref<Summary | null>(null)
const latestAlarm = ref<{ id: number; at: string } | null>(null)
let clock: ReturnType<typeof setInterval> | null = null
let unsubscribe: (() => void) | null = null

const navGroups = [
  { title: '监控中心', items: [
    { to: '/dashboard', match: (path: string) => path === '/dashboard', icon: DataBoard, label: '总览大屏' },
    { to: '/devices', match: (path: string) => path.startsWith('/devices'), icon: Monitor, label: '设备管理' },
    { to: '/health', match: (path: string) => path === '/health', icon: TrendCharts, label: '健康排行' },
    { to: '/alarms', match: (path: string) => path === '/alarms', icon: Bell, label: '告警中心' },
    { to: '/predictions', match: (path: string) => path === '/predictions', icon: DataAnalysis, label: '故障预测' },
    { to: '/reports', match: (path: string) => path === '/reports', icon: Files, label: '统计报表' },
  ] },
  { title: '智能运维', items: [
    { to: '/agent', match: (path: string) => path === '/agent', icon: ChatDotSquare, label: '智能助手' },
    { to: '/agent/schedules', match: (path: string) => path.startsWith('/agent/schedules'), icon: Calendar, label: '定期任务' },
  ] },
]

const userName = computed(() => currentUser.value?.real_name || currentUser.value?.username || '值班员')
const initial = computed(() => userName.value.slice(0, 2))
const dockOpen = computed(() => activeInspector.value !== null)
const clockText = computed(() => new Date(now.value).toLocaleTimeString('zh-CN', { hour12: false }))

interface DeepLink {
  key: string
  label: string
  icon: Component
  count: number | null
  note: string
  tone: string
  to: RouteLocationRaw
  active: boolean
  title: string
}

function hhmm(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '—' : at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

// 深层入口：直接落到已带筛选条件的视图，而不是与左侧导航重复的二级跳转
const deepLinks = computed<DeepLink[]>(() => {
  const query = route.query
  const onAlarms = route.path === '/alarms'
  const unknownHealth = summary.value?.health_unknown_count ?? 0
  return [
    {
      key: 'unhandled', label: '待处理告警', icon: Bell, count: activeAlarms.value, note: '', tone: '',
      to: { path: '/alarms', query: { status: 'unhandled' } },
      active: onAlarms && query.status === 'unhandled' && !query.level && !query.id,
      title: `打开告警中心 · 未处理（${activeAlarms.value} 条）`,
    },
    {
      key: 'urgent', label: '紧急未处理', icon: Warning, count: urgentAlarms.value, note: '', tone: 'danger',
      to: { path: '/alarms', query: { status: 'unhandled', level: 'urgent' } },
      active: onAlarms && query.level === 'urgent',
      title: `打开告警中心 · 紧急且未处理（${urgentAlarms.value} 条）`,
    },
    {
      key: 'offline', label: '离线设备', icon: Connection, count: summary.value?.offline ?? 0, note: '', tone: 'warn',
      to: { path: '/health', query: { state: 'offline' } },
      active: route.path === '/health' && query.state === 'offline',
      title: `打开健康排行 · 连接离线（${summary.value?.offline ?? 0} 台）`,
    },
    {
      key: 'health', label: '健康未知', icon: QuestionFilled, count: unknownHealth, note: '', tone: '',
      to: { path: '/health', query: { state: 'unknown' } },
      active: route.path === '/health' && query.state === 'unknown',
      title: `打开健康排行 · 数据不足（${unknownHealth} 台）`,
    },
    {
      key: 'latest', label: '最新告警', icon: Aim, count: null, note: latestAlarm.value ? hhmm(latestAlarm.value.at) : '—', tone: '',
      to: latestAlarm.value ? { path: '/alarms', query: { id: String(latestAlarm.value.id) } } : { path: '/alarms', query: { status: 'unhandled' } },
      active: onAlarms && Boolean(query.id),
      title: latestAlarm.value ? '直接打开最新一条未处理告警的详情' : '打开告警中心',
    },
  ]
})

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

async function refreshBadges(): Promise<void> {
  try {
    const result = await api.alarms(1, 1, 'unhandled')
    activeAlarms.value = result.total
    const first = result.list?.[0]
    latestAlarm.value = first ? { id: first.id, at: first.triggered_at } : null
  } catch { /* badge stays stale; visual only */ }
  try { urgentAlarms.value = (await api.alarms(1, 1, 'unhandled', 'urgent')).total } catch { /* same */ }
  try { summary.value = await api.summary() } catch { /* only feeds the deep-link counts */ }
}

onMounted(() => {
  clock = setInterval(() => { now.value = Date.now() }, 1000)
  void refreshBadges()
  unsubscribe = realtime.subscribe((event) => {
    if (event.type === 'alarm_created') {
      beep()
      void refreshBadges()
      ElNotification({
        title: `${levelLabels[event.data.level]}告警 · 设备 #${event.data.device_id}`,
        message: event.data.metric === 'ai_failure_risk' ? '预测风险升高，请核查设备状态' : `${event.data.metric} 越限，请及时核查`,
        type: event.data.level === 'urgent' ? 'error' : 'warning',
        duration: 9000,
        onClick: () => { void router.push({ path: '/alarms', query: { id: String(event.data.id) } }) },
      })
    }
    if (event.type === 'alarm_acked' || event.type === 'alarm_recovered') void refreshBadges()
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
    <nav class="rail" aria-label="主导航">
      <router-link class="rail-brand" to="/dashboard" aria-label="返回运行总览">
        <span class="brand-orb" aria-hidden="true">
          <svg viewBox="0 0 16 16"><path d="M9.4 1.2 3.5 8.8h3.2l-1 6 5.8-7.7H8.3Z" fill="currentColor" /></svg>
        </span>
      </router-link>
      <div class="rail-nav">
        <template v-for="(group, index) in navGroups" :key="group.title">
          <span v-if="index > 0" class="rail-sep" aria-hidden="true" />
          <router-link
            v-for="item in group.items" :key="item.to"
            class="rail-item" :class="{ active: item.match(route.path) }"
            :to="item.to" :title="`${group.title} · ${item.label}`" :aria-label="item.label"
            :aria-current="item.match(route.path) ? 'page' : undefined"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <em>{{ item.label }}</em>
            <i v-if="item.to === '/alarms' && activeAlarms" class="rail-badge">{{ activeAlarms > 99 ? '99+' : activeAlarms }}</i>
          </router-link>
        </template>
      </div>
      <div class="rail-foot">
        <span class="rail-avatar" :title="userName">{{ initial }}</span>
        <button class="rail-logout" type="button" title="退出登录" aria-label="退出登录" @click="logout">
          <el-icon><SwitchButton /></el-icon>
        </button>
      </div>
    </nav>

    <div class="app-col">
      <header class="statusbar">
        <span class="statusbar-crumb">
          <span class="crumb-root">源网智联</span>
          <span class="crumb-sep" aria-hidden="true">/</span>
          <strong>{{ route.meta.title || '运行工作台' }}</strong>
        </span>
        <nav class="deep-nav" aria-label="深层入口">
          <router-link
            v-for="link in deepLinks" :key="link.key"
            class="deep-link" :class="{ 'is-active': link.active }"
            :to="link.to" :title="link.title"
          >
            <el-icon><component :is="link.icon" /></el-icon>
            <span class="deep-text">{{ link.label }}</span>
            <b v-if="link.count !== null" class="deep-count" :class="{ danger: link.tone === 'danger' && link.count > 0, warn: link.tone === 'warn' && link.count > 0, zero: link.count === 0 }">{{ link.count }}</b>
            <em v-else class="deep-note">{{ link.note }}</em>
          </router-link>
        </nav>
        <div class="statusbar-tools">
          <div class="dock-triggers">
            <PageHelp />
            <InterpretationPanel />
          </div>
          <span class="statusbar-divider" aria-hidden="true" />
          <span class="meter" :class="realtime.connected.value ? 'ok' : 'off'" :title="realtime.connected.value ? '实时通道已连接' : '实时通道中断'">
            <span class="meter-dot" />{{ realtime.connected.value ? '实时' : '断线' }}
          </span>
          <span class="meter clock" :title="dateTime(now)">{{ clockText }}</span>
          <button class="meter meter-btn" type="button" :class="{ muted: !soundOn }" :title="soundOn ? '告警音已开启，点击静音' : '告警音已静音，点击开启'" :aria-label="soundOn ? '告警音已开启' : '告警音已静音'" :aria-pressed="soundOn" @click="toggleSound">
            <el-icon><BellFilled v-if="soundOn" /><MuteNotification v-else /></el-icon>
            {{ soundOn ? '开' : '关' }}
          </button>
        </div>
      </header>
      <div class="workspace">
        <div v-if="dockOpen" class="dock-scrim" aria-hidden="true" @click="closeInspector" />
        <main class="main-content"><router-view /></main>
        <aside class="dock" :class="{ open: dockOpen }" aria-label="检视栏">
          <div id="inspector-dock" class="dock-slot" />
        </aside>
      </div>
    </div>
  </div>
</template>
