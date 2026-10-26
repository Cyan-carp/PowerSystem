<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { errorMessage } from '../lib/format'
import { defaultScope, reconcileStation, scopeLabels, scopeOptions } from '../lib/scope'
import DeviceCode from '../components/DeviceCode.vue'
import DeviceName from '../components/DeviceName.vue'
import StationCode from '../components/StationCode.vue'
import type { Device, Scope, Station } from '../types'

const router = useRouter()
const rows = ref<Device[]>([])
const page = ref(1)
const total = ref(0)
const groupInput = ref('')
const group = ref('')
const scope = ref<Scope>(defaultScope)
const stations = ref<Station[]>([])
const station = ref('')
const stationsLoading = ref(false)
const stationError = ref('')
const loading = ref(false)
const error = ref('')
const pageSize = 20

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
  const requested = { scope: scope.value, station: station.value, group: group.value, page: page.value }
  loading.value = true
  error.value = ''
  try {
    const result = await api.devices(requested.page, pageSize, requested.group, { scope: requested.scope, stationCode: requested.station })
    if (requested.scope !== scope.value || requested.station !== station.value || requested.group !== group.value || requested.page !== page.value) return
    rows.value = result.list || []
    total.value = result.total
  } catch (cause) {
    if (requested.scope === scope.value) { error.value = errorMessage(cause); rows.value = [] }
  } finally { loading.value = false }
}
function search(): void { page.value = 1; group.value = groupInput.value.trim(); void load() }
function filter(): void { page.value = 1; void load() }
async function changeScope(next: Scope): Promise<void> {
  if (scope.value === next) return
  scope.value = next
  page.value = 1
  await loadStations()
  await load()
}
function reset(): void { groupInput.value = ''; group.value = ''; station.value = ''; page.value = 1; void load() }
onMounted(() => { void loadStations(); void load() })
</script>

<template>
  <div class="page-narrow">
    <section class="page-head"><div><h1>设备管理</h1><p>查看已登记设备和历史遥测记录。</p></div><div class="head-stat"><router-link to="/health">健康排行 →</router-link> · 筛选结果 <strong>{{ total }}</strong></div></section>
    <section class="surface">
      <div class="toolbar"><div class="toolbar-title">设备列表 <span>{{ scopeLabels[scope] }} · 按场站和设备分组筛选</span></div><div class="toolbar-actions">
        <el-button-group>
          <el-button v-for="item in scopeOptions" :key="item.value" size="small" :type="scope === item.value ? 'primary' : 'default'" @click="changeScope(item.value)">{{ item.label }}</el-button>
        </el-button-group>
        <el-select v-model="station" placeholder="全部场站" clearable :loading="stationsLoading" style="width: 210px" @change="filter">
          <el-option label="全部场站" value="" />
          <el-option v-for="item in stations" :key="item.station_code" :label="`${item.station_name}（${item.station_code}）`" :value="item.station_code" />
        </el-select>
        <el-input v-model="groupInput" placeholder="输入完整分组名称" clearable @keyup.enter="search" />
        <el-button type="primary" @click="search">筛选</el-button>
        <el-button @click="reset">重置</el-button>
      </div></div>
      <el-alert v-if="stationError" :title="stationError" type="warning" show-icon :closable="false" class="content-alert" />
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
      <el-table v-loading="loading" :data="rows" stripe empty-text="暂无符合条件的设备" class="data-table" @row-click="(row: Device) => router.push(`/devices/${row.id}`)">
        <el-table-column label="设备编号" min-width="160"><template #default="{ row }"><DeviceCode :code="row.device_code" :station="row.station_code" /></template></el-table-column>
        <el-table-column label="设备名称" min-width="260"><template #default="{ row }"><DeviceName :name="row.name" /></template></el-table-column>
        <el-table-column label="类型" prop="dev_type" min-width="105" />
        <el-table-column label="仿真参考型号" min-width="205"><template #default="{ row }">{{ row.is_simulated ? `${row.reference_vendor} ${row.reference_model}` : '—' }}</template></el-table-column>
        <el-table-column label="场站" min-width="110"><template #default="{ row }"><StationCode :code="row.station_code" /></template></el-table-column>
        <el-table-column label="分组" prop="group_name" min-width="115"><template #default="{ row }">{{ row.group_name || '未分组' }}</template></el-table-column>
        <el-table-column label="操作" width="115"><template #default="{ row }"><el-button link type="primary" @click.stop="router.push(`/devices/${row.id}`)">查看详情 →</el-button></template></el-table-column>
      </el-table>
      <div class="table-footer"><span>共 {{ total }} 台设备</span><el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" layout="prev, pager, next" @current-change="load" /></div>
    </section>
  </div>
</template>
