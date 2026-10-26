<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../lib/api'
import { dateTime, durationText, errorMessage } from '../lib/format'
import type { Device, TimelinessStat } from '../types'
import { useReportPanel, type ReportParams } from '../lib/report-panel'
import { usePaged } from '../lib/paged'
import DeviceCode from '../components/DeviceCode.vue'
import StationCode from '../components/StationCode.vue'
const now = new Date()
const range = ref<[Date, Date] | null>([new Date(now.getTime() - 7 * 86400000), now])
const station = ref(''), deviceID = ref<number | undefined>(), devices = ref<Device[]>([])
const availabilityPanel = useReportPanel(api.availability), timelinessPanel = useReportPanel(api.alarmTimeliness)
const availability = computed(() => availabilityPanel.data.value?.devices ?? [])
const stations = computed(() => availabilityPanel.data.value?.stations ?? [])
const timeliness = computed(() => timelinessPanel.data.value?.result ?? null)
const availabilityLoading = availabilityPanel.loading, availabilityError = availabilityPanel.error, availabilityLoaded = availabilityPanel.loaded
const timelinessLoading = timelinessPanel.loading, timelinessError = timelinessPanel.error, timelinessLoaded = timelinessPanel.loaded
const loading = computed(() => availabilityLoading.value || timelinessLoading.value)
const error = ref(''), exporting = ref<'availability'|'alarm-timeliness'|null>(null)
const stationCodes = computed(() => [...new Set(devices.value.map(d => d.station_code))].sort())
const deviceOptions = computed(() => devices.value.filter(d => !station.value || d.station_code === station.value))
// The availability report returns every device at once, so paging happens on
// the client. Each successful query is a new result set and starts at page 1.
const { page, pageSize, total, pageCount, rows, reset } = usePaged(() => availability.value, 20)
watch(() => availabilityPanel.data.value, () => reset())
const metrics = computed((): {name: string; value: TimelinessStat}[] => timeliness.value ? [
  {name:'告警确认耗时', value:timeliness.value.ack_timeliness}, {name:'平台告警恢复耗时（合计）', value:timeliness.value.recover_timeliness},
  {name:'遥测恢复耗时', value:timeliness.value.telemetry_recover_timeliness}, {name:'预测风险下降耗时', value:timeliness.value.prediction_recover_timeliness},
] : [])
const pct = (v: number | null) => v == null ? '—' : `${(100 * v).toFixed(2)}%`
const duration = durationText
async function load() {
  if (loading.value) return
  error.value = ''
  if (!range.value?.[0] || !range.value?.[1]) { error.value = '请选择完整时间范围'; return }
  const [start,end] = range.value
  if (end <= start || end.getTime() - start.getTime() > 31 * 86400000) { error.value = '时间范围应大于 0 且不超过 31 天'; return }
  const params: ReportParams = {start:start.toISOString(), end:end.toISOString(), station_code:station.value || undefined, device_id:deviceID.value}
  await Promise.all([availabilityPanel.load(params), timelinessPanel.load(params)])
}
async function download(kind: 'availability'|'alarm-timeliness') {
  const loaded = kind === 'availability' ? availabilityLoaded.value : timelinessLoaded.value
  if (!loaded || exporting.value) return
  exporting.value = kind; error.value = ''
  try { await api.exportReport(kind, loaded) } catch (cause) { error.value = errorMessage(cause) }
  finally { exporting.value = null }
}
onUnmounted(() => { availabilityPanel.cancel(); timelinessPanel.cancel() })

onMounted(async () => {
  void load()
  try {
    const all: Device[] = []
    for (let page=1; ; page++) { const result=await api.devices(page,100); all.push(...result.list); if (all.length>=result.total || !result.list.length) break }
    devices.value = all
  } catch(cause) { error.value = errorMessage(cause) }
})
</script>
<template>
  <div class="report-page">
    <section class="page-head"><div><h1>统计报表</h1><p>平台记录时间 · 最长 31 天</p></div></section>
    <section class="surface report-controls">
      <el-date-picker v-model="range" type="datetimerange" start-placeholder="开始" end-placeholder="结束" />
      <el-select v-model="station" clearable placeholder="全部场站" @change="deviceID=undefined"><el-option v-for="s in stationCodes" :key="s" :value="s" :label="s" /></el-select>
      <el-select v-model="deviceID" clearable filterable placeholder="全部设备"><el-option v-for="d in deviceOptions" :key="d.id" :value="d.id" :label="d.device_code" /></el-select>
      <el-button type="primary" :loading="loading" @click="load">查询</el-button>
    </section>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <details class="page-note"><summary>统计口径</summary><p>观测可用率＝覆盖的非故障时长 ÷ 覆盖时长。停机计入非故障，不表示正在发电；样本最多延续 15 秒，超出记未知。按设备存在时段计算有效窗口，按当前台账归组，不还原历史场站归属。覆盖率低于 90% 标记数据不足，不参加排行。</p></details>
    <section class="surface"><div class="surface-head"><h2>设备可用率</h2><el-button :disabled="!availabilityLoaded || !!exporting" :loading="exporting === 'availability'" @click="download('availability')">导出可用率 CSV</el-button></div>
      <p class="report-note in-panel" v-if="availabilityPanel.data.value">计算时间：{{ dateTime(availabilityPanel.data.value.computed_at) }}</p>
      <el-alert v-if="availabilityError" :title="availabilityError" type="error" :closable="false" />
      <el-table :data="rows" v-loading="availabilityLoading" stripe empty-text="无符合条件的设备">
        <el-table-column label="设备" min-width="160"><template #default="{row}"><DeviceCode :code="row.device_code" :station="row.station_code" /></template></el-table-column>
        <el-table-column label="场站" width="110"><template #default="{row}"><StationCode :code="row.station_code" /></template></el-table-column>
        <el-table-column label="观测可用率" min-width="115"><template #default="{row}">{{ pct(row.observed_availability) }}</template></el-table-column>
        <el-table-column label="数据覆盖率" min-width="115"><template #default="{row}">{{ pct(row.data_coverage) }}</template></el-table-column>
        <el-table-column v-for="(label,key) in {running_ms:'运行',stopped_ms:'停机',fault_ms:'故障',unknown_ms:'未知'}" :key="key" :label="label" min-width="135"><template #default="{row}">{{ duration(row[key]) }}</template></el-table-column>
        <el-table-column label="说明" width="100"><template #default="{row}">{{ row.insufficient_data ? '数据不足' : '有效' }}</template></el-table-column>
      </el-table>
      <div class="table-footer"><span>第 {{ page }} / {{ pageCount }} 页 · 共 {{ total }} 台设备</span><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="total" layout="prev, pager, next, sizes" /></div>
    </section>
    <section class="surface"><div class="surface-head"><h2>场站可用率</h2><p>按设备时长汇总后计算比率</p></div>
      <el-table v-loading="availabilityLoading" :data="stations" empty-text="暂无场站结果">
        <el-table-column label="场站"><template #default="{row}"><StationCode :code="row.station_code" /></template></el-table-column><el-table-column label="设备数" prop="device_count" />
        <el-table-column label="观测可用率"><template #default="{row}">{{ pct(row.observed_availability) }}</template></el-table-column>
        <el-table-column label="数据覆盖率"><template #default="{row}">{{ pct(row.data_coverage) }}</template></el-table-column>
        <el-table-column label="未知时长"><template #default="{row}">{{ duration(row.unknown_ms) }}</template></el-table-column>
        <el-table-column label="说明"><template #default="{row}">{{ row.insufficient_data ? '数据不足' : '有效' }}</template></el-table-column>
      </el-table>
    </section>
    <section class="surface"><div class="surface-head"><h2>告警处理时效</h2><el-button :disabled="!timelinessLoaded || !!exporting" :loading="exporting === 'alarm-timeliness'" @click="download('alarm-timeliness')">导出时效 CSV</el-button></div>
      <p class="report-note in-panel" v-if="timelinessPanel.data.value">计算时间：{{ dateTime(timelinessPanel.data.value.computed_at) }}</p>
      <el-alert v-if="timelinessError" :title="timelinessError" type="error" :closable="false" />
      <el-table v-loading="timelinessLoading" :data="metrics" empty-text="暂无结果">
        <el-table-column label="指标" prop="name" min-width="230" /><el-table-column label="有效样本数"><template #default="{row}">{{ row.value.count }}</template></el-table-column>
        <el-table-column label="平均值"><template #default="{row}">{{ duration(row.value.mean_ms) }}</template></el-table-column>
        <el-table-column label="中位数"><template #default="{row}">{{ duration(row.value.median_ms) }}</template></el-table-column>
        <el-table-column label="P90"><template #default="{row}">{{ duration(row.value.p90_ms) }}</template></el-table-column>
      </el-table>
      <p v-if="timeliness" class="report-note in-panel">事件 {{ timeliness.event_count }}；未确认 {{ timeliness.pending_ack }}（含未确认即结束）；未恢复 {{ timeliness.pending_recover }}；管理关闭 {{ timeliness.managed_recover }}；历史或原因未知 {{ timeliness.unknown_recover }}；异常生命周期 {{ timeliness.invalid_lifecycle }}。按窗口内触发的告警统计截至结束时间的状态，未完成不按零耗时计算；这些指标不是维修时长或 MTTR。</p>
    </section>
    <details class="page-note"><summary>导出说明</summary><p>CSV 使用最近一次成功查询的筛选范围，重新计算并记录其计算时间；数据在查询后更新时，导出数值可能随之变化。</p></details>
  </div>
</template>
<style scoped>.report-controls{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:16px}.report-controls .el-select{width:180px}.report-controls :deep(.el-date-editor){width:430px;flex:none}.report-note{color:var(--ink-3)}.report-note.in-panel{padding:0 16px 12px;margin:0;font-family:var(--font-num);font-size:10.5px;letter-spacing:.02em;color:var(--ink-3)}.report-page>.surface{margin-bottom:14px}</style>
