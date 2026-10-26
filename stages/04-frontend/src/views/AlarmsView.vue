<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FolderAdd, View } from '@element-plus/icons-vue'
import { api } from '../lib/api'
import AlarmValueComparison from '../components/AlarmValueComparison.vue'
import { alarmClosure } from '../lib/health'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, levelLabels, metricUnits, statusLabels } from '../lib/format'
import { fixedNumber } from '../lib/detailPresentation'
import { alarmMetricLabel, alarmOriginLabel, isLegacySimulationMetric, legacySimulationNote, normalAlarmSamplingNote } from '../lib/interpretation-display'
import { defaultScope, reconcileStation, scopeLabels, scopeOptions } from '../lib/scope'
import type { Alarm, AlarmLevel, AlarmStatus, Metric, Scope, Station } from '../types'

const route = useRoute()
const router = useRouter()
const rows = ref<Alarm[]>([])
const selected = ref<Alarm | null>(null)
const drawer = ref(false)
const page = ref(1)
const total = ref(0)
const level = ref<AlarmLevel | ''>('')
const status = ref<AlarmStatus | ''>('')
const origin = ref<'normal' | 'simulation' | ''>('')
const scope = ref<Scope>(defaultScope)
const stations = ref<Station[]>([])
const station = ref('')
const stationsLoading = ref(false)
const stationError = ref('')
const loading = ref(false)
const acting = ref(false)
const error = ref('')
const detailError = ref('')
let unsubscribe: (() => void) | null = null
let periodic: ReturnType<typeof setInterval> | null = null

const QUERY_STATUSES: AlarmStatus[] = ['unhandled', 'acked', 'recovered']
const QUERY_LEVELS: AlarmLevel[] = ['urgent', 'major', 'minor']
// 深层入口带参进入时把查询参数当初始筛选；参数被清空也要同步回来，避免筛选与地址不一致
function applyQueryFilters(): boolean {
  const query = route.query
  const nextStatus = typeof query.status === 'string' && (QUERY_STATUSES as string[]).includes(query.status) ? query.status as AlarmStatus : ''
  const nextLevel = typeof query.level === 'string' && (QUERY_LEVELS as string[]).includes(query.level) ? query.level as AlarmLevel : ''
  if (status.value === nextStatus && level.value === nextLevel) return false
  status.value = nextStatus
  level.value = nextLevel
  page.value = 1
  return true
}

async function loadStations(): Promise<void> {
  stationsLoading.value = true
  stationError.value = ''
  try {
    const result = await api.stations(scope.value)
    if (result.scope !== scope.value) return
    stations.value = result.list || []
    station.value = reconcileStation(station.value, scope.value, stations.value)
  } catch (cause) {
    stations.value = []
    station.value = ''
    stationError.value = `场站列表暂不可用：${errorMessage(cause)}`
  } finally { stationsLoading.value = false }
}

async function load(): Promise<void> {
  const requested = { scope: scope.value, station: station.value, status: status.value, level: level.value, page: page.value, origin: origin.value }
  loading.value = true
  error.value = ''
  try {
    const result = await api.alarms(requested.page, 20, requested.status, requested.level, { scope: requested.scope, stationCode: requested.station, origin: requested.origin || undefined })
    if (requested.scope !== scope.value || requested.station !== station.value || requested.status !== status.value || requested.level !== level.value || requested.page !== page.value || requested.origin !== origin.value) return
    rows.value = result.list || []
    total.value = result.total
  } catch (cause) { if (requested.scope === scope.value) error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function openAlarm(id: number): Promise<void> {
  drawer.value = true
  detailError.value = ''
  selected.value = null
  try { selected.value = await api.alarm(id) }
  catch (cause) { detailError.value = errorMessage(cause) }
}

async function acknowledge(alarm: Alarm): Promise<void> {
  acting.value = true
  try {
    selected.value = await api.ackAlarm(alarm.id)
    ElMessage.success('告警已确认')
  } catch (cause) {
    ElMessage.warning(errorMessage(cause))
    try { selected.value = await api.alarm(alarm.id) } catch { /* list refresh below */ }
  } finally { acting.value = false; void load() }
}

async function archive(alarm: Alarm): Promise<void> {
  try {
    await ElMessageBox.confirm('归档后该告警将从默认列表中隐藏，原始记录仍会保留。', `归档告警 #${alarm.id}`, {
      type: 'warning', confirmButtonText: '归档', cancelButtonText: '取消',
    })
  } catch { return }
  acting.value = true
  try {
    await api.archiveAlarm(alarm.id)
    drawer.value = false
    selected.value = null
    ElMessage.success('告警已归档')
    if (rows.value.length === 1 && page.value > 1) page.value--
    await load()
  } catch (cause) {
    ElMessage.warning(errorMessage(cause))
  } finally { acting.value = false }
}

const checked = ref<Alarm[]>([])
const archiving = ref(false)
async function archiveSelected(): Promise<void> {
  const ids = checked.value.map(row => row.id)
  if (!ids.length || archiving.value) return
  try { await ElMessageBox.confirm(`将归档 ${ids.length} 条告警，归档后不再出现在本列表。`, '批量归档', { type: 'warning', confirmButtonText: '归档' }) } catch { return }
  archiving.value = true
  try {
    const result = await api.archiveAlarms(ids)
    checked.value = []
    await load()
    if (result.failed.length) ElMessage.error(`${result.failed.length} 条告警归档失败，请刷新后重试`)
    else ElMessage.success(`已归档 ${result.archived} 条告警`)
  } catch (cause) { ElMessage.warning(errorMessage(cause)) }
  finally { archiving.value = false }
}
function onRowClick(row: Alarm, column?: { type?: string }): void { if (column?.type === 'selection') return; void openAlarm(row.id) }

function filter(): void { page.value = 1; void load() }
async function changeScope(next: Scope): Promise<void> {
  if (scope.value === next) return
  scope.value = next
  page.value = 1
  await loadStations()
  await load()
}
watch(() => route.query.id, (id) => { const parsed = Number(id); if (Number.isInteger(parsed) && parsed > 0) void openAlarm(parsed) }, { immediate: true })
watch(() => [route.query.status, route.query.level], () => { if (applyQueryFilters()) void load() })
onMounted(() => {
  applyQueryFilters()
  void loadStations()
  void load()
  unsubscribe = realtime.subscribe((event) => {
    if (event.type === 'connected' || event.type === 'alarm_created' || event.type === 'alarm_acked' || event.type === 'alarm_recovered') {
      void load()
      if (selected.value && 'data' in event && event.data.id === selected.value.id) void openAlarm(selected.value.id)
    }
  })
  periodic = setInterval(() => { if (!realtime.connected.value) void load() }, 10000)
})
onUnmounted(() => { unsubscribe?.(); if (periodic) clearInterval(periodic) })
</script>

<template>
  <div class="page-narrow">
  <section class="page-head"><div><h1>告警中心</h1><p>按级别、状态查找告警，并完成确认闭环。</p></div><div class="head-stat">筛选结果 <strong>{{ total }}</strong></div></section>
  <section class="surface"><div class="toolbar"><div class="toolbar-title">告警记录 <span>{{ scopeLabels[scope] }} · 新事件会实时同步</span></div><div class="toolbar-actions"><el-button-group><el-button v-for="item in scopeOptions" :key="item.value" size="small" :type="scope === item.value ? 'primary' : 'default'" @click="changeScope(item.value)">{{ item.label }}</el-button></el-button-group><el-select v-model="station" placeholder="全部场站" clearable :loading="stationsLoading" style="width: 190px" @change="filter"><el-option label="全部场站" value="" /><el-option v-for="item in stations" :key="item.station_code" :label="`${item.station_name}（${item.station_code}）`" :value="item.station_code" /></el-select><el-select v-model="origin" placeholder="全部来源" clearable style="width: 145px" @change="filter"><el-option label="全部来源" value="" /><el-option label="自然规则告警" value="normal" /><el-option label="历史演示告警" value="simulation" /></el-select><el-select v-model="level" placeholder="全部级别" style="width: 125px" @change="filter"><el-option label="全部级别" value="" /><el-option v-for="(label, value) in levelLabels" :key="value" :label="label" :value="value" /></el-select><el-select v-model="status" placeholder="全部状态" style="width: 125px" @change="filter"><el-option label="全部状态" value="" /><el-option v-for="(label, value) in statusLabels" :key="value" :label="label" :value="value" /></el-select><el-button :disabled="!checked.length || archiving" :loading="archiving" @click="archiveSelected">批量归档{{ checked.length ? ` (${checked.length})` : '' }}</el-button></div></div>
    <details class="panel-note"><summary>数据口径</summary><p>规则告警由设备遥测越限自动生成，来源可能是合成车队设备或真实设备。{{ normalAlarmSamplingNote }}历史演示记录保留，可按来源筛选。</p></details>
    <el-alert v-if="stationError" :title="stationError" type="warning" show-icon :closable="false" class="content-alert" />
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
    <el-table v-loading="loading" :data="rows" stripe empty-text="暂无符合条件的告警" class="data-table" @row-click="onRowClick" @selection-change="(rows: Alarm[]) => (checked = rows)"><el-table-column type="selection" width="46" align="center" header-align="center" /><el-table-column label="级别" width="95" align="center" header-align="center"><template #default="{ row }"><span class="severity-label" :class="row.level"><span class="severity-dot" :class="row.level" />{{ levelLabels[row.level as AlarmLevel] }}</span></template></el-table-column><el-table-column label="告警内容" min-width="210" align="left" header-align="left"><template #default="{ row }"><strong>{{ isLegacySimulationMetric(row.metric) ? alarmMetricLabel(row.metric) : row.metric === 'ai_failure_risk' ? 'AI 故障风险' : alarmMetricLabel(row.metric) + ' 越限' }}</strong><small class="alarm-origin">{{ alarmOriginLabel(row.origin) }}</small></template></el-table-column><el-table-column label="触发值 / 阈值" width="155" align="center" header-align="center"><template #default="{ row }"><span v-if="isLegacySimulationMetric(row.metric)" class="alarm-values-missing">—</span><span v-else class="alarm-values">{{ fixedNumber(row.value) }}{{ metricUnits[row.metric as Metric] || '' }} / {{ fixedNumber(row.threshold) }}{{ metricUnits[row.metric as Metric] || '' }}</span></template></el-table-column><el-table-column label="设备 ID" prop="device_id" width="110" align="center" header-align="center" /><el-table-column label="触发时间" min-width="175" align="center" header-align="center"><template #default="{ row }">{{ dateTime(row.triggered_at) }}</template></el-table-column><el-table-column label="状态" width="100" align="center" header-align="center"><template #default="{ row }"><span class="alarm-status" :class="row.status">{{ statusLabels[row.status as AlarmStatus] }}</span></template></el-table-column><el-table-column label="" width="96" align="center" header-align="center"><template #default="{ row }"><div class="row-actions"><el-button link title="查看详情" aria-label="查看详情" @click.stop="openAlarm(row.id)"><el-icon><View /></el-icon></el-button><el-button link title="归档" aria-label="归档" :loading="acting" @click.stop="archive(row)"><el-icon><FolderAdd /></el-icon></el-button></div></template></el-table-column></el-table>
    <div class="table-footer"><span>共 {{ total }} 条记录</span><el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" /></div>
  </section>
  </div>
  <el-drawer v-model="drawer" title="告警详情" size="min(520px, 100vw)" @closed="router.replace({ query: {} })">
    <el-alert v-if="detailError" :title="detailError" type="error" show-icon :closable="false" />
    <div v-if="selected" class="drawer-content"><div class="drawer-severity"><span class="severity-dot" :class="selected.level" />{{ levelLabels[selected.level] }}告警 <el-tag :type="selected.status === 'unhandled' ? 'danger' : selected.status === 'acked' ? 'warning' : 'success'">{{ statusLabels[selected.status] }}</el-tag></div><h2>{{ isLegacySimulationMetric(selected.metric) ? alarmMetricLabel(selected.metric) : selected.metric === 'ai_failure_risk' ? 'AI 故障风险升高' : alarmMetricLabel(selected.metric) + ' 指标越限' }}</h2><p v-if="isLegacySimulationMetric(selected.metric)" class="alarm-source-note">{{ legacySimulationNote }}</p><template v-else><AlarmValueComparison :value="selected.value" :threshold="selected.threshold" :metric="selected.metric" /><p class="alarm-source-note">{{ normalAlarmSamplingNote }}</p></template>
      <h3 class="detail-section-heading">记录信息</h3>
      <dl class="alarm-record-meta"><dt>告警编号</dt><dd class="record-identity">#{{ selected.id }}</dd><dt>设备 ID</dt><dd class="record-identity">{{ selected.device_id }}</dd><dt>触发时间</dt><dd>{{ dateTime(selected.triggered_at) }}</dd><dt>确认时间</dt><dd>{{ dateTime(selected.acked_at) }}</dd><dt>平台结束时间</dt><dd>{{ dateTime(selected.recovered_at) }}</dd></dl><dl class="alarm-record-meta"><dt>结束原因</dt><dd>{{ selected.status === 'recovered' ? alarmClosure(selected.close_reason) : '尚未结束' }}</dd><dt>触发规则快照</dt><dd>{{ selected.rule_operator || '未知比较符' }} · {{ selected.rule_version || '历史版本未知' }}</dd></dl><div class="drawer-actions"><el-button v-if="selected.status === 'unhandled'" type="primary" :loading="acting" @click="acknowledge(selected)">确认告警</el-button><el-button type="danger" plain :loading="acting" @click="archive(selected)">归档告警</el-button><el-button @click="router.push(`/devices/${selected?.device_id}`)">查看关联设备曲线 →</el-button></div><p class="drawer-note">归档只会从默认列表隐藏记录，不删除数据。确认仅记录操作人与时间，不代表恢复；管理关闭也不证明故障消除。</p></div>
  </el-drawer>
</template>


<style scoped>
.data-table :deep(.severity-label){justify-content:center}
.alarm-status{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600}
.alarm-status::before{content:'';flex:none;width:7px;height:7px;border-radius:2px;background:var(--ink-3)}
.alarm-status.unhandled{color:var(--danger)}
.alarm-status.unhandled::before{background:var(--danger)}
.alarm-status.acked{color:var(--warning)}
.alarm-status.acked::before{background:var(--warning)}
.alarm-status.recovered{color:var(--success)}
.alarm-status.recovered::before{background:var(--success)}
.row-actions{display:flex;justify-content:center;gap:2px}
.row-actions .el-button{padding:4px 6px;font-size:15px;color:var(--ink-2)}
.row-actions .el-button:hover{color:var(--brand)}
.alarm-origin{display:block;margin-top:4px;font-size:11px;font-weight:400;color:var(--ink-3)}
.alarm-values{font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.alarm-values-missing{color:var(--ink-3)}
.alarm-source-note{margin:10px 0 0;padding:8px 10px;border-left:3px solid var(--line);background:var(--card-2);font-size:12px;line-height:1.7;color:var(--ink-3)}
</style>
