<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { loadHistory } from '../lib/history'
import { chinaDayStart, dailyGeneration } from '../lib/daily-generation'
import type { DailyPoint, Sample } from '../lib/daily-generation'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, levelLabels } from '../lib/format'
import DashboardTrend from '../components/DashboardTrend.vue'
import type { Alarm, Device, Summary } from '../types'

const router = useRouter()
const summary = ref<Summary | null>(null)
const devices = ref<Device[]>([])
const alarms = ref<Alarm[]>([])
const powerTrend = ref<DailyPoint[]>([])
const energyTrend = ref<DailyPoint[]>([])
const loading = ref(true)
const error = ref('')
const dailyError = ref('')
const dailyLoading = ref(false)
const dailyUpdated = ref<number | null>(null)
const lastUpdate = ref<Date | null>(null)
const dailyStart = ref(chinaDayStart(Date.now()))
let raw = new Map<number, Sample[]>()
let queriedUntil = 0
let lastDailyAttempt = 0
let unsubscribe: (() => void) | null = null
let periodic: ReturnType<typeof setInterval> | null = null
let summaryTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false
const integer = (value: number | null | undefined): string => value != null && Number.isFinite(value)
  ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value) : '—'

async function refreshStatus(): Promise<void> {
  try {
    const [current, recent] = await Promise.all([api.summary(), api.alarms(1, 6)])
    if (disposed) return
    summary.value = current
    alarms.value = recent.list || []
    lastUpdate.value = new Date()
    error.value = ''
  } catch (cause) { if (!disposed) error.value = errorMessage(cause) }
}

async function refreshDaily(): Promise<void> {
  if (dailyLoading.value || !devices.value.length || disposed) return
  dailyLoading.value = true
  const end = Date.now()
  const start = chinaDayStart(end)
  lastDailyAttempt = end
  if (start !== dailyStart.value) {
    dailyStart.value = start
    raw = new Map(); queriedUntil = 0
    powerTrend.value = []; energyTrend.value = []; dailyUpdated.value = null
  }
  const from = Math.max(start, queriedUntil ? queriedUntil - 15_000 : start)
  try {
    // Keep raw five-second samples for energy integration; downsampling first loses valid segments.
    const result = await Promise.all(devices.value.map(async (device) => ({
      id: device.id, points: await loadHistory(device.id, 'power', new Date(from), new Date(end), Infinity),
    })))
    if (disposed || chinaDayStart(Date.now()) !== start) return
    for (const item of result) {
      const merged = new Map((raw.get(item.id) ?? []).map((point) => [point[0], point[1]]))
      for (const [time, value] of item.points) merged.set(time, value)
      raw.set(item.id, [...merged].sort((a, b) => a[0] - b[0]))
    }
    const daily = dailyGeneration([...raw.values()], start, end)
    powerTrend.value = daily.power; energyTrend.value = daily.energy
    queriedUntil = end; dailyUpdated.value = end; dailyError.value = ''
  } catch (cause) { if (!disposed) dailyError.value = `今日历史数据暂不可用：${errorMessage(cause)}` }
  finally { dailyLoading.value = false }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [page] = await Promise.all([api.devices(1, 100), refreshStatus()])
    let list = page.list || []
    for (let pageNumber = 2; list.length < page.total; pageNumber++) {
      const next = await api.devices(pageNumber, 100)
      if (!next.list?.length) break
      list = [...list, ...next.list]
    }
    if (disposed) return
    devices.value = list
    await refreshDaily()
  } catch (cause) { if (!disposed) error.value = errorMessage(cause) }
  finally { loading.value = false }
}
function scheduleStatus(): void {
  if (summaryTimer) return
  summaryTimer = setTimeout(() => { summaryTimer = null; void refreshStatus() }, 1200)
}
onMounted(() => {
  void load()
  unsubscribe = realtime.subscribe((event) => {
    if (event.type === 'connected' || event.type.startsWith('alarm_')) void refreshStatus()
    if (event.type === 'telemetry') scheduleStatus()
  })
  periodic = setInterval(() => {
    void refreshStatus()
    if (Date.now() - lastDailyAttempt >= 5 * 60_000 || chinaDayStart(Date.now()) !== dailyStart.value) void refreshDaily()
  }, 10000)
})
onUnmounted(() => { disposed = true; unsubscribe?.(); if (periodic) clearInterval(periodic); if (summaryTimer) clearTimeout(summaryTimer) })
</script>

<template>
  <section class="page-head dashboard-head"><div><h1>运行总览</h1></div><div class="head-meta"><span class="live-pill"><span class="signal-dot" />实时监测</span><small>最近同步 {{ dateTime(lastUpdate?.getTime()) }}</small></div></section>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
  <div v-loading="loading" class="dashboard-content">
    <section class="surface stat-band" aria-label="运行状态统计">
      <div class="stat-cell"><span class="stat-label">登记设备</span><strong>{{ summary?.device_total ?? '—' }}</strong><em>台</em><small>在线 {{ summary?.online ?? '—' }} · 离线 {{ summary?.offline ?? '—' }}</small></div>
      <div class="stat-cell ok"><span class="stat-label">在线设备</span><strong>{{ summary?.online ?? '—' }}</strong><em>台</em><small>故障 {{ summary?.fault ?? '—' }} 台</small></div>
      <div class="stat-cell warn"><span class="stat-label">活动告警</span><strong>{{ summary?.active_alarms ?? '—' }}</strong><em>条</em><small>未闭环事件实时计入</small></div>
      <div class="stat-cell"><span class="stat-label">运行正常占比</span><strong>{{ summary?.operational_health_percent?.toFixed(0) ?? '—' }}<em>%</em></strong><small>依据当前在线状态</small></div>
      <div class="stat-cell"><span class="stat-label">设备健康度</span><strong>{{ summary?.health_score_percent?.toFixed(0) ?? '—' }}<em v-if="summary?.health_score_percent != null">分</em></strong><small>依据在线、故障与活动告警</small></div>
    </section>
    <section class="surface gauge-panel" aria-label="发电监测">
      <div class="surface-head"><h2>发电监测</h2><span>今日 · 北京时间</span></div>
      <div class="generation-totals" aria-label="实时发电数据">
        <div class="trend-heading"><span>全站当前功率</span><strong>{{ integer(summary?.current_power_kw) }} <small>kW</small></strong></div>
        <div class="trend-heading"><span>当日累计发电量</span><strong>{{ integer(summary?.today_energy_kwh) }} <small>kWh</small></strong></div>
      </div>
      <div v-if="dailyError" class="daily-status"><span>{{ dailyError }}</span><el-button link type="primary" :loading="dailyLoading" @click="refreshDaily">重新加载</el-button></div>
      <div class="gauge-grid" v-loading="dailyLoading && !dailyUpdated">
        <div class="gauge-cell"><div class="daily-chart-title"><h3>今日发电功率</h3><span>5 分钟均值</span></div><DashboardTrend :points="powerTrend" kind="power" /></div>
        <div class="gauge-cell"><div class="daily-chart-title"><h3>今日分时发电量</h3><span>每小时发电量</span></div><DashboardTrend :points="energyTrend" kind="energy" /></div>
      </div>
      <p class="trend-note">全天 0–24 点，每 4 小时一个刻度。基于设备当日历史采样，缺测和未到时段留空；发电量按有效功率片段积分。<span v-if="dailyUpdated"> 数据截至 {{ dateTime(dailyUpdated) }}，每 5 分钟更新。</span></p>
    </section>
    <section class="surface dashboard-alarm"><div class="surface-head"><h2>最新告警</h2><el-button link type="primary" @click="router.push('/alarms')">查看全部 →</el-button></div><div v-if="alarms.length" class="alarm-feed"><button v-for="alarm in alarms" :key="alarm.id" class="alarm-feed-row" @click="router.push({ path: '/alarms', query: { id: String(alarm.id) } })"><span class="severity-dot" :class="alarm.level" /><span class="feed-main"><strong>{{ alarm.metric === 'ai_failure_risk' ? 'AI 故障风险' : alarm.metric + ' 越限' }}</strong><small>设备 #{{ alarm.device_id }} · {{ dateTime(alarm.triggered_at) }}</small></span><span class="feed-status">{{ levelLabels[alarm.level] }}</span></button></div><div v-else class="empty-box">当前没有告警记录。</div></section>
    <section class="surface device-overview"><div class="surface-head"><h2>设备速览</h2><el-button link type="primary" @click="router.push('/devices')">设备列表 →</el-button></div><div v-if="devices.length" class="device-chip-grid"><button v-for="device in devices.slice(0, 12)" :key="device.id" class="device-chip" @click="router.push(`/devices/${device.id}`)"><span class="device-chip-icon">{{ device.device_code.slice(-2) }}</span><span><strong>{{ device.name }}</strong><small>{{ device.device_code }} · {{ device.station_code }}</small></span><span class="chip-arrow">→</span></button></div><div v-else class="empty-box">尚无已登记设备。</div></section>
  </div>
</template>
