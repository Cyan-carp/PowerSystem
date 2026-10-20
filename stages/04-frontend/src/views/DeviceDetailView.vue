<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../lib/api'
import { loadHistory } from '../lib/history'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, metricLabels, metricUnits } from '../lib/format'
import { healthBasis, healthState } from '../lib/health'
import TelemetryChart from '../components/TelemetryChart.vue'
import type { Device, DeviceHealth, Metric, Telemetry } from '../types'

const route = useRoute()
const router = useRouter()
const device = ref<Device | null>(null)
const latest = ref<Telemetry | null>(null)
const health = ref<DeviceHealth | null>(null)
const healthComputed = ref('')
const exporting = ref(false)
let healthTimer: ReturnType<typeof setInterval> | undefined
let healthBusy = false
async function refreshHealth() {
  if (healthBusy) return
  healthBusy = true
  const id = deviceId()
  try { const result=await api.healthDevices(); if (id === deviceId()) { health.value=result.devices.find(d=>d.device_id===id) ?? null; healthComputed.value=result.computed_at } }
  catch { if (id === deviceId()) health.value=null }
  finally { healthBusy=false }
}
const metric = ref<Metric>('temperature')
const dateRange = ref<[Date, Date]>([new Date(Date.now() - 60 * 60 * 1000), new Date()])
const points = ref<[number, number][]>([])
const loading = ref(false)
const error = ref('')
const chartError = ref('')
const presetActive = ref(1)
const exportError = ref('')
let unsubscribe: (() => void) | null = null
let requestId = 0

function deviceId(): number { return Number(route.params.id) }

async function loadDevice(): Promise<void> {
  error.value = ''
  const id = deviceId()
  if (!Number.isInteger(id) || id < 1) { error.value = '设备编号无效'; return }
  try { device.value = await api.device(id) }
  catch (cause) { error.value = errorMessage(cause); return }
  try { latest.value = await api.latest(id) }
  catch { latest.value = null }
  await refreshHealth()
  await loadChart()
}

async function exportCsv(): Promise<void> {
  exportError.value = ''
  if (!dateRange.value) { exportError.value = '请先选择时间范围'; return }
  const [start, end] = dateRange.value
  if (!start || !end) { exportError.value = '请先选择时间范围'; return }
  if (end.getTime() - start.getTime() > 7 * 24 * 3600 * 1000) { exportError.value = '导出窗口最长 7 天'; return }
  exporting.value = true
  try { await api.exportTelemetry(deviceId(), start, end) }
  catch (cause) { exportError.value = errorMessage(cause) }
  finally { exporting.value = false }
}

async function loadChart(): Promise<void> {
  if (!device.value || !dateRange.value || dateRange.value.length !== 2) return
  const sequence = ++requestId
  loading.value = true
  chartError.value = ''
  try {
    const result = await loadHistory(deviceId(), metric.value, dateRange.value[0], dateRange.value[1])
    if (sequence === requestId) points.value = result
  } catch (cause) {
    if (sequence === requestId) { chartError.value = errorMessage(cause); points.value = [] }
  } finally { if (sequence === requestId) loading.value = false }
}

function preset(hours: number): void {
  const now = Date.now()
  presetActive.value = hours
  dateRange.value = [new Date(now - hours * 60 * 60 * 1000), new Date(now)]
  void loadChart()
}

function customRange(): void { presetActive.value = 0; void loadChart() }

watch(() => route.params.id, () => { device.value = null; latest.value = null; void loadDevice() })
onMounted(() => {
  void loadDevice()
  healthTimer = setInterval(refreshHealth, 10000)
  unsubscribe = realtime.subscribe((event) => {
    if (event.type !== 'telemetry' || event.data.device_id !== deviceId()) return
    latest.value = event.data
    if (dateRange.value[1].getTime() < Date.now() - 60000) return
    const value = event.data[metric.value]
    points.value = [...points.value.filter(([time]) => time !== event.data.ts_ms), [event.data.ts_ms, value] as [number, number]].sort((a, b) => a[0] - b[0]).slice(-1800)
  })
})
onUnmounted(() => { unsubscribe?.(); if (healthTimer) clearInterval(healthTimer) })
</script>

<template>
  <div class="back-link" @click="router.push('/devices')">← 返回设备列表</div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
  <template v-if="device">
    <section class="page-head"><div><h1>{{ device.name }}</h1><p>{{ device.device_code }} · {{ device.station_code }} · {{ device.group_name || '未分组' }} · {{ device.dev_type }}</p></div><el-tag type="info" size="large">设备 ID {{ device.id }}</el-tag></section>
    <div class="detail-grid">
      <section class="surface detail-info"><div class="surface-head"><h2>设备档案</h2></div><dl><dt>设备编号</dt><dd>{{ device.device_code }}</dd><dt>设备类型</dt><dd>{{ device.dev_type }}</dd><dt>所属场站</dt><dd>{{ device.station_code }}</dd><dt>厂商</dt><dd>{{ device.vendor || '未填写' }}</dd><template v-if="device.is_simulated"><dt>设备性质</dt><dd>仿真设备，遥测并非真机数据</dd><dt>仿真参考型号</dt><dd>{{ device.reference_vendor }} {{ device.reference_model }}</dd></template><dt>分组</dt><dd>{{ device.group_name || '未分组' }}</dd></dl></section>
      <section class="surface health-info"><div class="surface-head"><h2>健康评分</h2><span v-if="health">{{ health.rule_version }}</span></div><div v-if="health" class="health-box"><div class="health-score" :class="{ na: health.score == null }"><strong>{{ health.score == null ? '未知' : health.score }}</strong><small>/ 100</small></div><dl class="health-meta"><dt>状态</dt><dd>{{ healthState(health.state) }}</dd><dt>评分依据</dt><dd>{{ healthBasis(health.matched_by) }}</dd><dt>计算时间</dt><dd>{{ dateTime(healthComputed) }}</dd><dt>采样时间</dt><dd>{{ dateTime(health.sampled_at_ms) }}</dd><dt>接收时间</dt><dd>{{ dateTime(health.received_at) }}</dd><dt>活动告警级别</dt><dd>{{ health.active_level || '无' }}</dd></dl></div><div v-else class="empty-box">暂无评分数据。</div></section>
      <section class="surface latest-info"><div class="surface-head"><h2>最新遥测</h2><span>{{ latest ? dateTime(latest.received_at) : '暂无数据' }}</span></div><div v-if="latest" class="latest-grid"><div><span>温度</span><strong>{{ latest.temperature.toFixed(1) }}<small>°C</small></strong></div><div><span>功率</span><strong>{{ latest.power.toFixed(1) }}<small>kW</small></strong></div><div><span>电压</span><strong>{{ latest.voltage.toFixed(1) }}<small>V</small></strong></div><div><span>电流</span><strong>{{ latest.current.toFixed(1) }}<small>A</small></strong></div></div><div v-else class="empty-box">设备尚无最新遥测；检查模拟器与数据链路。</div></section>
    </div>
    <section class="surface chart-surface"><div class="surface-head"><div><h2>历史遥测曲线</h2><p>选择指标和时间范围，查询 TDengine 中的{{ device.is_simulated ? '仿真' : '已存储' }}数据。</p></div><span class="chart-count">{{ points.length }} 个绘图点</span></div>
      <div class="chart-controls"><el-select v-model="metric" style="width: 140px" aria-label="选择指标" @change="loadChart"><el-option v-for="(label, key) in metricLabels" :key="key" :label="`${label} (${metricUnits[key]})`" :value="key" /></el-select><div class="seg-group"><el-button :class="{ 'is-active': presetActive === 0.25 }" @click="preset(0.25)">15 分钟</el-button><el-button :class="{ 'is-active': presetActive === 1 }" @click="preset(1)">1 小时</el-button><el-button :class="{ 'is-active': presetActive === 4 }" @click="preset(4)">4 小时</el-button><el-button :class="{ 'is-active': presetActive === 24 }" @click="preset(24)">24 小时</el-button></div><el-date-picker v-model="dateRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" format="YYYY.M.D HH:mm" @change="customRange" /><el-button type="primary" :loading="loading" @click="loadChart">查询</el-button><el-button :loading="exporting" @click="exportCsv">导出 CSV</el-button></div>
      <el-alert v-if="chartError" :title="chartError" type="error" show-icon :closable="false" class="content-alert" />
      <el-alert v-if="exportError" :title="exportError" type="error" show-icon :closable="false" class="content-alert" />
      <div v-loading="loading"><TelemetryChart v-if="points.length" :points="points" :metric="metric" /><div v-else class="empty-box tall">当前时间范围暂无遥测点</div></div>
    </section>
  </template>
</template>

<style scoped>.health-box{padding:18px}.health-score strong{font-size:32px}.health-score small{color:var(--ink-3);margin-left:8px}.health-meta{display:grid;grid-template-columns:90px 1fr;gap:10px;font-size:13px}.health-meta dt{color:var(--ink-3)}.health-meta dd{margin:0}</style>
