<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { loadHistory } from '../lib/history'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, levelLabels } from '../lib/format'
import TelemetryChart from '../components/TelemetryChart.vue'
import type { Alarm, Device, Summary } from '../types'

const router = useRouter()
const summary = ref<Summary | null>(null)
const devices = ref<Device[]>([])
const alarms = ref<Alarm[]>([])
const selectedId = ref<number | null>(null)
const points = ref<[number, number][]>([])
const loading = ref(true)
const error = ref('')
const lastUpdate = ref<Date | null>(null)
let unsubscribe: (() => void) | null = null
let periodic: ReturnType<typeof setInterval> | null = null
let summaryTimer: ReturnType<typeof setTimeout> | null = null
let chartSequence = 0
let userSelectedDevice = false

const selectedDevice = computed(() => devices.value.find((item) => item.id === selectedId.value) || null)

async function refreshStatus(): Promise<void> {
  try {
    const [current, recent] = await Promise.all([api.summary(), api.alarms(1, 6)])
    summary.value = current
    alarms.value = recent.list || []
    lastUpdate.value = new Date()
    error.value = ''
  } catch (cause) { error.value = errorMessage(cause) }
}

async function refreshChart(): Promise<void> {
  if (!selectedId.value) return
  const seq = ++chartSequence
  try {
    const end = new Date()
    const result = await loadHistory(selectedId.value, 'power', new Date(end.getTime() - 5 * 60 * 1000), end)
    if (seq === chartSequence) points.value = result
  } catch (cause) { if (seq === chartSequence) error.value = errorMessage(cause) }
}

function choose(id: number): void { userSelectedDevice = true; selectedId.value = id; points.value = []; void refreshChart() }

async function chooseRecentDevice(items: Device[]): Promise<void> {
  const latest = await Promise.allSettled(items.map((item) => api.latest(item.id)))
  if (userSelectedDevice) return
  const newest = latest.flatMap((result, index) => result.status === 'fulfilled'
    ? [{ id: items[index].id, at: Date.parse(result.value.received_at) }] : [])
    .filter((item) => Number.isFinite(item.at) && item.at >= Date.now() - 5 * 60 * 1000)
    .sort((a, b) => b.at - a.at)[0]
  if (newest) selectedId.value = newest.id
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [page] = await Promise.all([api.devices(1, 100), refreshStatus()])
    devices.value = page.list || []
    if (devices.value.length && !selectedId.value) selectedId.value = devices.value[0].id
    await chooseRecentDevice(devices.value)
    await refreshChart()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function scheduleStatus(): void {
  if (summaryTimer) return
  summaryTimer = setTimeout(() => { summaryTimer = null; void refreshStatus() }, 1200)
}

onMounted(() => {
  void load()
  unsubscribe = realtime.subscribe((event) => {
    if (event.type === 'connected') { void refreshStatus(); void refreshChart(); return }
    if (event.type === 'telemetry') {
      scheduleStatus()
      if (!userSelectedDevice && !points.value.length && devices.value.some((item) => item.id === event.data.device_id)) selectedId.value = event.data.device_id
      if (event.data.device_id === selectedId.value) {
        const sample: [number, number] = [event.data.ts_ms, event.data.power]
        points.value = [...points.value.filter(([time]) => time !== sample[0]), sample]
          .filter(([time]) => time >= Date.now() - 5 * 60 * 1000)
          .sort((a, b) => a[0] - b[0]).slice(-150)
      }
    }
    if (event.type.startsWith('alarm_')) void refreshStatus()
  })
  periodic = setInterval(() => { void refreshStatus(); if (!realtime.connected.value) void refreshChart() }, 10000)
})
onUnmounted(() => { unsubscribe?.(); if (periodic) clearInterval(periodic); if (summaryTimer) clearTimeout(summaryTimer) })
</script>

<template>
  <section class="page-head dashboard-head"><div><span class="section-kicker">OPERATION OVERVIEW</span><h1>运行总览</h1><p>实时感知场站设备、功率与告警动态。</p></div><div class="head-meta"><span class="live-pill"><span class="signal-dot" />实时监测</span><small>最近同步 {{ dateTime(lastUpdate?.getTime()) }}</small></div></section>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
  <div v-loading="loading" class="dashboard-content">
    <section class="kpi-grid">
      <div class="kpi-card primary"><span>登记设备</span><strong>{{ summary?.device_total ?? '—' }}</strong><small>台设备</small></div>
      <div class="kpi-card"><span>在线设备</span><strong>{{ summary?.online ?? '—' }}</strong><small>{{ summary?.offline ?? '—' }} 台离线</small></div>
      <div class="kpi-card"><span>当前功率</span><strong>{{ summary?.current_power_kw?.toFixed(1) ?? '—' }}</strong><small>kW · 非累计发电量</small></div>
      <div class="kpi-card"><span>当日发电量</span><strong>{{ summary?.today_energy_kwh?.toFixed(1) ?? '—' }}</strong><small>kWh · 留存累计 {{ summary?.retained_energy_kwh?.toFixed(1) ?? '—' }} kWh</small></div>
      <div class="kpi-card warning"><span>活动告警</span><strong>{{ summary?.active_alarms ?? '—' }}</strong><small>{{ summary?.fault ?? '—' }} 台故障设备</small></div>
      <div class="kpi-card"><span>运行正常占比</span><strong>{{ summary?.operational_health_percent?.toFixed(0) ?? '—' }}<em>%</em></strong><small>依据当前在线状态</small></div>
      <div class="kpi-card"><span>设备健康度</span><strong>{{ summary?.health_score_percent?.toFixed(0) ?? '—' }}<em v-if="summary?.health_score_percent != null">分</em></strong><small>依据在线、故障与活动告警</small></div>
    </section>
    <div class="dashboard-grid">
      <section class="surface realtime-panel"><div class="surface-head"><div><span class="section-kicker">LIVE TELEMETRY</span><h2>实时功率趋势</h2></div><el-select v-if="devices.length" v-model="selectedId" style="width: 210px" aria-label="选择设备" @change="choose"><el-option v-for="device in devices" :key="device.id" :label="device.name" :value="device.id" /></el-select></div><div class="chart-context"><span>{{ selectedDevice?.device_code || '暂无设备' }}</span><span>近 5 分钟 · kW</span></div><TelemetryChart v-if="points.length" :points="points" metric="power" height="330px" /><div v-else class="empty-box tall">暂无实时曲线数据；等待设备上报。</div></section>
      <section class="surface dashboard-alarm"><div class="surface-head"><div><span class="section-kicker">ALARM FEED</span><h2>最新告警</h2></div><el-button link type="primary" @click="router.push('/alarms')">查看全部 →</el-button></div><div v-if="alarms.length" class="alarm-feed"><button v-for="alarm in alarms" :key="alarm.id" class="alarm-feed-row" @click="router.push({ path: '/alarms', query: { id: String(alarm.id) } })"><span class="severity-dot" :class="alarm.level" /><span class="feed-main"><strong>{{ alarm.metric === 'ai_failure_risk' ? 'AI 故障风险' : alarm.metric + ' 越限' }}</strong><small>设备 #{{ alarm.device_id }} · {{ dateTime(alarm.triggered_at) }}</small></span><span class="feed-status">{{ levelLabels[alarm.level] }}</span></button></div><div v-else class="empty-box">当前没有告警记录。</div></section>
    </div>
    <section class="surface device-overview"><div class="surface-head"><div><span class="section-kicker">DEVICE FLEET</span><h2>设备速览</h2></div><el-button link type="primary" @click="router.push('/devices')">设备列表 →</el-button></div><div v-if="devices.length" class="device-chip-grid"><button v-for="device in devices.slice(0, 12)" :key="device.id" class="device-chip" @click="router.push(`/devices/${device.id}`)"><span class="device-chip-icon">{{ device.device_code.slice(-2) }}</span><span><strong>{{ device.name }}</strong><small>{{ device.device_code }} · {{ device.station_code }}</small></span><span class="chip-arrow">→</span></button></div><div v-else class="empty-box">尚无已登记设备。</div></section>
  </div>
</template>
