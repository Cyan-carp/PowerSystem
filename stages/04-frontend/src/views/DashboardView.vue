<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { loadHistory } from '../lib/history'
import { chinaDayStart, dailyGeneration } from '../lib/daily-generation'
import type { DailyPoint, Sample } from '../lib/daily-generation'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, levelLabels } from '../lib/format'
import { allowsDailyCurve, defaultScope, scopeLabels, scopeOptions } from '../lib/scope'
import DashboardTrend from '../components/DashboardTrend.vue'
import type { Alarm, Device, Overview, Scope } from '../types'

const router = useRouter()
const scope = ref<Scope>(defaultScope)
const overview = ref<Overview | null>(null)
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
let dailyEpoch = 0
let unsubscribe: (() => void) | null = null
let periodic: ReturnType<typeof setInterval> | null = null
let summaryTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false

const summary = computed(() => overview.value?.summary ?? null)
const stations = computed(() => overview.value?.stations ?? [])
const rangeLabel = computed(() => scopeLabels[scope.value])
const curveEnabled = computed(() => allowsDailyCurve(scope.value))

const integer = (value: number | null | undefined): string => value != null && Number.isFinite(value)
  ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value) : '—'

function resetDaily(): void {
  dailyEpoch++
  raw = new Map()
  queriedUntil = 0
  powerTrend.value = []
  energyTrend.value = []
  dailyUpdated.value = null
  dailyError.value = ''
  dailyLoading.value = false
  dailyStart.value = chinaDayStart(Date.now())
}

async function refreshStatus(): Promise<void> {
  const requested = scope.value
  try {
    const [current, recent] = await Promise.all([
      api.overview(requested),
      api.alarms(1, 6, '', '', { scope: requested }),
    ])
    if (disposed || requested !== scope.value) return
    overview.value = current
    alarms.value = recent.list || []
    lastUpdate.value = new Date()
    error.value = ''
  } catch (cause) { if (!disposed && requested === scope.value) error.value = errorMessage(cause) }
}

async function refreshDaily(): Promise<void> {
  if (!curveEnabled.value || dailyLoading.value || !devices.value.length || disposed) return
  const requested = scope.value
  const epoch = dailyEpoch
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
    if (disposed || requested !== scope.value || epoch !== dailyEpoch || !curveEnabled.value) return
    if (chinaDayStart(Date.now()) !== start) return
    for (const item of result) {
      const merged = new Map((raw.get(item.id) ?? []).map((point) => [point[0], point[1]]))
      for (const [time, value] of item.points) merged.set(time, value)
      raw.set(item.id, [...merged].sort((a, b) => a[0] - b[0]))
    }
    const daily = dailyGeneration([...raw.values()], start, end)
    powerTrend.value = daily.power; energyTrend.value = daily.energy
    queriedUntil = end; dailyUpdated.value = end; dailyError.value = ''
  } catch (cause) { if (!disposed && requested === scope.value && epoch === dailyEpoch) dailyError.value = `今日历史数据暂不可用：${errorMessage(cause)}` }
  finally { if (requested === scope.value && epoch === dailyEpoch) dailyLoading.value = false }
}

async function load(): Promise<void> {
  const requested = scope.value
  loading.value = true
  error.value = ''
  resetDaily()
  try {
    // Business/all only need a bounded device preview; the baseline needs all three curve devices.
    const pageSize = requested === 'baseline' ? 100 : 12
    const [page] = await Promise.all([api.devices(1, pageSize, '', { scope: requested }), refreshStatus()])
    let list = page.list || []
    // Only the baseline scope needs every device for its three-device daily curves.
    if (requested === 'baseline') {
      for (let pageNumber = 2; list.length < page.total; pageNumber++) {
        const next = await api.devices(pageNumber, 100, '', { scope: requested })
        if (!next.list?.length) break
        list = [...list, ...next.list]
      }
    }
    if (disposed || requested !== scope.value) return
    devices.value = list
    if (requested === 'baseline') await refreshDaily()
  } catch (cause) { if (!disposed && requested === scope.value) error.value = errorMessage(cause) }
  finally { if (requested === scope.value) loading.value = false }
}

function switchScope(next: Scope): void {
  if (scope.value === next) return
  scope.value = next
  overview.value = null
  devices.value = []
  alarms.value = []
  resetDaily()
  void load()
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
    if (curveEnabled.value && (Date.now() - lastDailyAttempt >= 5 * 60_000 || chinaDayStart(Date.now()) !== dailyStart.value)) void refreshDaily()
  }, 10000)
})
onUnmounted(() => { disposed = true; unsubscribe?.(); if (periodic) clearInterval(periodic); if (summaryTimer) clearTimeout(summaryTimer) })
</script>

<template>
  <section class="page-head dashboard-head">
    <div><h1>运行总览</h1><p>{{ rangeLabel }} · 期望 {{ overview?.expected_device_count ?? '—' }} 台 · 登记 {{ overview?.registered_device_count ?? '—' }} 台 · 已观测 {{ overview?.observed_device_count ?? '—' }} 台</p></div>
    <div class="head-meta">
      <el-button-group>
        <el-button v-for="item in scopeOptions" :key="item.value" size="small" :type="scope === item.value ? 'primary' : 'default'" @click="switchScope(item.value)">{{ item.label }}</el-button>
      </el-button-group>
      <span class="live-pill"><span class="signal-dot" />实时监测</span>
      <small>最近同步 {{ dateTime(lastUpdate?.getTime()) }}</small>
    </div>
  </section>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
  <el-alert v-if="overview?.partial" :title="`${rangeLabel}数据暂不完整：期望 ${overview.expected_device_count} 台，登记 ${overview.registered_device_count} 台，已观测 ${overview.observed_device_count} 台`" description="部分设备尚未导入台账、未接入供数或当前窗口内没有有效遥测；缺失值以 — 显示，不按 0 计算。" type="warning" show-icon :closable="false" class="content-alert" />
  <div v-loading="loading" class="dashboard-content">
    <section class="surface stat-band" aria-label="运行状态统计">
      <div class="stat-cell"><span class="stat-label">登记设备</span><strong>{{ summary?.device_total ?? '—' }}</strong><em>台</em><small>期望 {{ overview?.expected_device_count ?? '—' }} · 已观测 {{ overview?.observed_device_count ?? '—' }}</small></div>
      <div class="stat-cell ok"><span class="stat-label">在线设备</span><strong>{{ summary?.online ?? '—' }}</strong><em>台</em><small>离线 {{ summary?.offline ?? '—' }} · 故障 {{ summary?.fault ?? '—' }} 台</small></div>
      <div class="stat-cell warn"><span class="stat-label">活动告警</span><strong>{{ summary?.active_alarms ?? '—' }}</strong><em>条</em><small>未闭环事件实时计入</small></div>
      <div class="stat-cell"><span class="stat-label">运行正常占比</span><strong>{{ summary?.operational_health_percent?.toFixed(0) ?? '—' }}<em v-if="summary?.operational_health_percent != null">%</em></strong><small>依据当前在线状态</small></div>
      <div class="stat-cell"><span class="stat-label">设备健康度</span><strong>{{ summary?.health_score_percent?.toFixed(0) ?? '—' }}<em v-if="summary?.health_score_percent != null">分</em></strong><small>{{ summary?.health_provisional ? `暂定 · 未知 ${summary.health_unknown_count} 台` : '依据在线、故障与活动告警' }} <router-link to="/health">评分依据 →</router-link></small></div>
    </section>
    <section class="surface gauge-panel" aria-label="发电监测">
      <div class="surface-head"><h2>发电监测</h2><span>{{ rangeLabel }} · 今日 · 北京时间</span></div>
      <div class="generation-totals" aria-label="实时发电数据">
        <div class="trend-heading"><span>当前范围功率</span><strong>{{ integer(summary?.current_power_kw) }} <small>kW</small></strong></div>
        <div class="trend-heading"><span>当日累计发电量</span><strong>{{ integer(summary?.today_energy_kwh) }} <small>kWh</small></strong></div>
      </div>
      <div v-if="!curveEnabled" class="curve-pending">
        <strong>分时聚合后续提供</strong>
        <p>{{ rangeLabel }}（{{ overview?.registered_device_count ?? '—' }} 台设备）不逐台加载全天历史曲线。当前功率与当日累计发电量来自服务端总览聚合；分时功率与电量曲线将在服务端分时聚合接口上线后提供。</p>
      </div>
      <template v-else>
        <div v-if="dailyError" class="daily-status"><span>{{ dailyError }}</span><el-button link type="primary" :loading="dailyLoading" @click="refreshDaily">重新加载</el-button></div>
        <div class="gauge-grid" v-loading="dailyLoading && !dailyUpdated">
          <div class="gauge-cell"><div class="daily-chart-title"><h3>今日发电功率</h3><span>5 分钟均值</span></div><DashboardTrend :points="powerTrend" kind="power" /></div>
          <div class="gauge-cell"><div class="daily-chart-title"><h3>今日分时发电量</h3><span>每小时发电量</span></div><DashboardTrend :points="energyTrend" kind="energy" /></div>
        </div>
        <p class="trend-note">全天 0–24 点，每 4 小时一个刻度。基于 ST-01 三台设备当日历史采样，缺测和未到时段留空；发电量按有效功率片段积分。<span v-if="dailyUpdated"> 数据截至 {{ dateTime(dailyUpdated) }}，每 5 分钟更新。</span></p>
      </template>
    </section>
    <section class="surface station-compare" aria-label="场站对比">
      <div class="surface-head"><h2>场站对比</h2><span>{{ rangeLabel }} · {{ stations.length }} 个场站</span></div>
      <el-table :data="stations" class="data-table" empty-text="当前范围暂无场站">
        <el-table-column label="场站" min-width="210">
          <template #default="{ row }"><strong>{{ row.station_name }}</strong><span class="cell-sub">{{ row.station_code }} · {{ row.scope === 'baseline' ? '基线' : '业务' }}</span></template>
        </el-table-column>
        <el-table-column label="登记 / 期望" width="120"><template #default="{ row }">{{ row.registered_device_count }} / {{ row.expected_device_count }}</template></el-table-column>
        <el-table-column label="已观测" width="90"><template #default="{ row }">{{ row.observed_device_count }}</template></el-table-column>
        <el-table-column label="在线" width="80"><template #default="{ row }">{{ row.online }}</template></el-table-column>
        <el-table-column label="活动告警" width="100"><template #default="{ row }">{{ row.active_alarms }}</template></el-table-column>
        <el-table-column label="当前功率" width="125"><template #default="{ row }">{{ integer(row.current_power_kw) }} <small>kW</small></template></el-table-column>
        <el-table-column label="健康度" width="95"><template #default="{ row }">{{ row.health_score_percent != null ? row.health_score_percent.toFixed(0) : '—' }}<span v-if="row.health_score_percent != null"> 分</span></template></el-table-column>
        <el-table-column label="完整性" width="100"><template #default="{ row }"><el-tag v-if="row.partial" type="warning" size="small">部分</el-tag><el-tag v-else type="success" size="small">完整</el-tag></template></el-table-column>
      </el-table>
      <p class="trend-note">登记为台账设备数，期望为共享清单设备数，已观测为当前 15 秒窗口内有有效遥测的设备数。partial 表示登记少于期望，或有效观测少于已登记数；缺失的功率、电量或健康度显示为 —。</p>
    </section>
    <section class="surface dashboard-alarm"><div class="surface-head"><h2>最新告警</h2><el-button link type="primary" @click="router.push('/alarms')">查看全部 →</el-button></div><div v-if="alarms.length" class="alarm-feed"><button v-for="alarm in alarms" :key="alarm.id" class="alarm-feed-row" @click="router.push({ path: '/alarms', query: { id: String(alarm.id) } })"><span class="severity-dot" :class="alarm.level" /><span class="feed-main"><strong>{{ alarm.metric === 'ai_failure_risk' ? 'AI 故障风险' : alarm.metric + ' 越限' }}</strong><small>设备 #{{ alarm.device_id }} · {{ dateTime(alarm.triggered_at) }}</small></span><span class="feed-status">{{ levelLabels[alarm.level] }}</span></button></div><div v-else class="empty-box">当前范围没有告警记录。</div></section>
    <section class="surface device-overview"><div class="surface-head"><h2>设备速览</h2><span>{{ rangeLabel }} · 前 12 台</span><el-button link type="primary" @click="router.push('/devices')">设备列表 →</el-button></div><div v-if="devices.length" class="device-chip-grid"><button v-for="device in devices.slice(0, 12)" :key="device.id" class="device-chip" @click="router.push(`/devices/${device.id}`)"><span class="device-chip-icon">{{ device.device_code.slice(-2) }}</span><span><strong>{{ device.name }}</strong><small>{{ device.device_code }} · {{ device.station_code }}</small></span><span class="chip-arrow">→</span></button></div><div v-else class="empty-box">当前范围尚无已登记设备。</div></section>
  </div>
</template>
