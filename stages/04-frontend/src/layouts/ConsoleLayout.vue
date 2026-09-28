<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElNotification } from 'element-plus'
import { Bell, DataAnalysis, DataBoard, Monitor, Mute, SwitchButton } from '@element-plus/icons-vue'
import { clearSession, currentUser } from '../lib/auth'
import { realtime } from '../lib/realtime'
import { dateTime, levelLabels } from '../lib/format'

const route = useRoute()
const router = useRouter()
const now = ref(Date.now())
const soundOn = ref(sessionStorage.getItem('powersystem_sound') !== 'off')
let clock: ReturnType<typeof setInterval> | null = null
let unsubscribe: (() => void) | null = null

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

onMounted(() => {
  clock = setInterval(() => { now.value = Date.now() }, 1000)
  unsubscribe = realtime.subscribe((event) => {
    if (event.type !== 'alarm_created') return
    beep()
    ElNotification({
      title: `${levelLabels[event.data.level]}告警 · 设备 #${event.data.device_id}`,
      message: event.data.metric === 'ai_failure_risk' ? '预测风险升高，请核查设备状态' : `${event.data.metric} 越限，请及时核查`,
      type: event.data.level === 'urgent' ? 'error' : 'warning',
      duration: 9000,
      onClick: () => { void router.push({ path: '/alarms', query: { id: String(event.data.id) } }) },
    })
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
    <aside class="sidebar">
      <router-link class="brand" to="/dashboard">
        <span class="brand-mark">源</span>
        <span><strong>源网智联</strong><small>新能源设备智能运维</small></span>
      </router-link>
      <div class="nav-caption">运行工作台</div>
      <nav class="side-nav" aria-label="主导航">
        <router-link to="/dashboard" :class="{ active: route.path === '/dashboard' }"><el-icon><DataBoard /></el-icon><span>总览大屏</span></router-link>
        <router-link to="/devices" :class="{ active: route.path.startsWith('/devices') }"><el-icon><Monitor /></el-icon><span>设备管理</span></router-link>
        <router-link to="/alarms" :class="{ active: route.path === '/alarms' }"><el-icon><Bell /></el-icon><span>告警中心</span></router-link>
        <router-link to="/predictions" :class="{ active: route.path === '/predictions' }"><el-icon><DataAnalysis /></el-icon><span>故障预测</span></router-link>
      </nav>
      <div class="sidebar-foot"><span class="signal-dot" />本地运行环境</div>
    </aside>
    <div class="main-column">
      <header class="topbar">
        <div class="topbar-context"><span>源网智联</span><span class="slash">/</span><strong>{{ route.path === '/dashboard' ? '运行总览' : route.path.startsWith('/devices') ? '设备管理' : route.path === '/alarms' ? '告警中心' : '故障预测' }}</strong></div>
        <div class="topbar-actions">
          <span class="clock">{{ dateTime(now) }}</span>
          <span class="connection" :class="realtime.connected.value ? 'online' : 'offline'"><span class="signal-dot" />{{ realtime.connected.value ? '实时连接正常' : '实时连接中断' }}</span>
          <el-button text :aria-label="soundOn ? '静音提示音' : '启用提示音'" @click="toggleSound"><el-icon><Bell v-if="soundOn" /><Mute v-else /></el-icon></el-button>
          <span class="user-name">{{ currentUser?.real_name || currentUser?.username || '值班员' }}</span>
          <el-button text title="退出登录" aria-label="退出登录" @click="logout"><el-icon><SwitchButton /></el-icon></el-button>
        </div>
      </header>
      <main class="main-content"><router-view /></main>
    </div>
  </div>
</template>
