<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../lib/api'
import { loadHistory } from '../lib/history'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, metricLabels, metricUnits } from '../lib/format'
import TelemetryChart from '../components/TelemetryChart.vue'
import type { Device, Metric, Telemetry } from '../types'

const route = useRoute()
const router = useRouter()
const device = ref<Device | null>(null)
const latest = ref<Telemetry | null>(null)
const metric = ref<Metric>('temperature')
const dateRange = ref<[Date, Date]>([new Date(Date.now() - 60 * 60 * 1000), new Date()])
const points = ref<[number, number][]>([])
const loading = ref(false)
const error = ref('')
const chartError = ref('')
const presetActive = ref(1)
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
  await loadChart()
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
  unsubscribe = realtime.subscribe((event) => {
    if (event.type !== 'telemetry' || event.data.device_id !== deviceId()) return
    latest.value = event.data
    if (dateRange.value[1].getTime() < Date.now() - 60000) return
    const value = event.data[metric.value]
    points.value = [...points.value.filter(([time]) => time !== event.data.ts_ms), [event.data.ts_ms, value] as [number, number]].sort((a, b) => a[0] - b[0]).slice(-1800)
  })
})
onUnmounted(() => unsubscribe?.())
</script>

<template>
  <div class="back-link" @click="router.push('/devices')">← 返回设备列表</div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
  <template v-if="device">
    <section class="page-head"><div><span class="section-kicker">设备详情 / {{ device.device_code }}</span><h1>{{ device.name }}</h1><p>{{ device.station_code }} · {{ device.group_name || '未分组' }} · {{ device.dev_type }}</p></div><el-tag type="info" size="large">设备 ID {{ device.id }}</el-tag></section>
    <div class="detail-grid">
      <section class="surface detail-info"><div class="surface-head"><h2>设备档案</h2></div><dl><dt>设备编号</dt><dd>{{ device.device_code }}</dd><dt>设备类型</dt><dd>{{ device.dev_type }}</dd><dt>所属场站</dt><dd>{{ device.station_code }}</dd><dt>厂商</dt><dd>{{ device.vendor || '未填写' }}</dd><dt>分组</dt><dd>{{ device.group_name || '未分组' }}</dd></dl></section>
      <section class="surface latest-info"><div class="surface-head"><h2>最新遥测</h2><span>{{ latest ? dateTime(latest.received_at) : '暂无数据' }}</span></div><div v-if="latest" class="latest-grid"><div><span>温度</span><strong>{{ latest.temperature.toFixed(1) }}<small>°C</small></strong></div><div><span>功率</span><strong>{{ latest.power.toFixed(1) }}<small>kW</small></strong></div><div><span>电压</span><strong>{{ latest.voltage.toFixed(1) }}<small>V</small></strong></div><div><span>电流</span><strong>{{ latest.current.toFixed(1) }}<small>A</small></strong></div></div><div v-else class="empty-box">设备尚无最新遥测；检查模拟器与数据链路。</div></section>
    </div>
    <section class="surface chart-surface"><div class="surface-head"><div><h2>历史遥测曲线</h2><p>选择指标和时间范围，查询 TDengine 中的真实数据。</p></div><span class="chart-count">{{ points.length }} 个绘图点</span></div>
      <div class="chart-controls"><el-select v-model="metric" style="width: 140px" aria-label="选择指标" @change="loadChart"><el-option v-for="(label, key) in metricLabels" :key="key" :label="`${label} (${metricUnits[key]})`" :value="key" /></el-select><div class="seg-group"><el-button :class="{ 'is-active': presetActive === 0.25 }" @click="preset(0.25)">15 分钟</el-button><el-button :class="{ 'is-active': presetActive === 1 }" @click="preset(1)">1 小时</el-button><el-button :class="{ 'is-active': presetActive === 4 }" @click="preset(4)">4 小时</el-button><el-button :class="{ 'is-active': presetActive === 24 }" @click="preset(24)">24 小时</el-button></div><el-date-picker v-model="dateRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" format="YYYY-MM-DD HH:mm" @change="customRange" /><el-button type="primary" :loading="loading" @click="loadChart">查询</el-button></div>
      <el-alert v-if="chartError" :title="chartError" type="error" show-icon :closable="false" class="content-alert" />
      <div v-loading="loading"><TelemetryChart v-if="points.length" :points="points" :metric="metric" /><div v-else class="empty-box tall">当前时间范围暂无遥测点</div></div>
    </section>
  </template>
</template>
