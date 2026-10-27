<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api'
import { useReconnectRefresh } from '../lib/reconnect-refresh'
import { dateTime, errorMessage, levelLabels } from '../lib/format'
import { healthBasis, healthState } from '../lib/health'
import { usePaged } from '../lib/paged'
import DeviceCode from '../components/DeviceCode.vue'
import StationCode from '../components/StationCode.vue'
import type { DeviceHealth, StationHealth, AlarmLevel } from '../types'
const devices = ref<DeviceHealth[]>([]), stations = ref<StationHealth[]>([])
const computedAt = ref(''), rule = ref(''), error = ref(''), loading = ref(false)
const keyword = ref(''), station = ref('')
const state = ref<'' | 'scored' | 'unknown' | 'offline' | 'fault'>('')
const route = useRoute()
const STATE_QUERY = ['unknown', 'scored', 'offline', 'fault'] as const
// 深层入口 ?state=unknown / offline 直接落到对应批次；地址被清空时同步回全部
function applyQueryState(): void {
  const raw = route.query.state
  const next = typeof raw === 'string' && (STATE_QUERY as readonly string[]).includes(raw) ? raw as typeof state.value : ''
  if (state.value !== next) state.value = next
}
applyQueryState()
watch(() => route.query.state, applyQueryState)
let timer: ReturnType<typeof setInterval> | undefined, disposed = false
async function load() {
  if (loading.value) return
  loading.value = true
  try {
    const data = await api.healthDevices()
    if (disposed) return
    devices.value = data.devices; stations.value = data.stations; computedAt.value = data.computed_at; rule.value = data.rule_version; error.value = ''
  } catch (cause) { if (!disposed) { error.value = errorMessage(cause); devices.value = []; stations.value = [] } }
  finally { loading.value = false }
}
const stationCodes = computed(() => [...new Set(devices.value.map(d => d.station_code))].sort())
const isUnknown = (d: DeviceHealth): boolean => d.state === 'unknown' || d.score === null
const matchesState = (d: DeviceHealth): boolean => !state.value
  || (state.value === 'unknown' ? isUnknown(d) : state.value === 'scored' ? !isUnknown(d) : d.state === state.value)
const matched = computed(() => {
  const kw = keyword.value.trim().toLowerCase(), code = station.value
  return devices.value.filter(d => (!code || d.station_code === code)
    && matchesState(d)
    && (!kw || d.device_code.toLowerCase().includes(kw) || (d.name || '').toLowerCase().includes(kw)))
})
const { page, pageSize, total, pageCount, rows, reset } = usePaged(() => matched.value, 20)
// A filter change starts a new result set: paging must go back to the top.
watch([keyword, station, state], () => reset())
// Auto refresh can retire a station, which would otherwise leave an empty table.
watch(stationCodes, codes => { if (station.value && !codes.includes(station.value)) station.value = '' })
onMounted(() => { void load(); timer = setInterval(load, 10000) })
useReconnectRefresh(load)
onUnmounted(() => { disposed = true; if (timer) clearInterval(timer) })
</script>
<template>
  <div>
    <section class="page-head"><div><h1>设备与场站健康排行</h1><p>{{ rule }} · 计算时间 {{ dateTime(computedAt) }} · 每 10 秒刷新</p></div><el-button :loading="loading" @click="load">刷新</el-button></section>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <details class="page-note"><summary>评分规则</summary><p>当前规则评分：离线或遥测故障 0 分，在线非故障按最高活动告警取 40 / 70 / 90 / 100 分。未知不参与排名；离线扣分不证明硬件故障。按当前台账归组，同分同名次。</p></details>
    <section class="surface"><div class="surface-head"><h2>设备排行</h2><div class="head-filters">
        <el-select v-model="station" clearable placeholder="全部场站" class="station-select"><el-option v-for="s in stationCodes" :key="s" :value="s" :label="s" /></el-select>
        <el-select v-model="state" placeholder="全部状态" class="state-select"><el-option label="全部状态" value="" /><el-option label="有评分" value="scored" /><el-option label="数据不足" value="unknown" /><el-option label="连接离线" value="offline" /><el-option label="遥测故障" value="fault" /></el-select>
        <el-input v-model="keyword" placeholder="设备编号 / 名称" clearable class="keyword-input" />
      </div></div>
      <el-table :data="rows" v-loading="loading" stripe empty-text="暂无评分结果">
        <el-table-column label="名次" width="70"><template #default="{row}">{{ row.rank ?? '—' }}</template></el-table-column>
        <el-table-column label="设备" min-width="170"><template #default="{row}"><router-link :to="`/devices/${row.device_id}`"><DeviceCode :code="row.device_code" :station="row.station_code" /></router-link></template></el-table-column>
        <el-table-column label="场站" width="115"><template #default="{row}"><StationCode :code="row.station_code" /></template></el-table-column>
        <el-table-column label="分数" width="80"><template #default="{row}">{{ row.score ?? '未知' }}</template></el-table-column>
        <el-table-column label="状态" min-width="115"><template #default="{row}">{{ healthState(row.state) }}</template></el-table-column>
        <el-table-column label="评分依据" min-width="240"><template #default="{row}">{{ healthBasis(row.matched_by) }}<span v-if="row.active_level"> · {{ levelLabels[row.active_level as AlarmLevel] }}</span></template></el-table-column>
        <el-table-column label="采样时间（北京）" min-width="175"><template #default="{row}">{{ dateTime(row.sampled_at_ms) }}</template></el-table-column>
      </el-table>
      <div class="table-footer"><span>第 {{ page }} / {{ pageCount }} 页 · 共 {{ total }} 台设备</span><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]" :total="total" layout="prev, pager, next, sizes" /></div>
    </section>
    <section class="surface health-stations"><div class="surface-head"><h2>场站排行</h2><p>有效设备等权平均；有未知设备时为暂定分，无正式名次。</p></div>
      <el-table :data="stations" empty-text="暂无场站">
        <el-table-column label="名次" width="70"><template #default="{row}">{{ row.rank ?? '—' }}</template></el-table-column>
        <el-table-column label="场站" prop="station_code" min-width="120" />
        <el-table-column label="分数" min-width="100"><template #default="{row}">{{ row.score == null ? '未知' : row.score.toFixed(2) }}</template></el-table-column>
        <el-table-column label="设备总数" prop="device_total" /><el-table-column label="有效" prop="valid_count" /><el-table-column label="未知" prop="unknown_count" />
        <el-table-column label="说明" min-width="150"><template #default="{row}">{{ row.provisional ? '暂定分，不参加正式排行' : '正式' }}</template></el-table-column>
      </el-table>
    </section>
  </div>
</template>
<style scoped>.health-stations{margin-top:18px}.head-filters{display:flex;align-items:center;gap:8px}.station-select{width:150px}.state-select{width:120px}.keyword-input{width:190px}</style>
