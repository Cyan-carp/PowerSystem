<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../lib/api'
import { dateTime, errorMessage, levelLabels } from '../lib/format'
import { healthBasis, healthState } from '../lib/health'
import type { DeviceHealth, StationHealth, AlarmLevel } from '../types'
const devices = ref<DeviceHealth[]>([]), stations = ref<StationHealth[]>([])
const computedAt = ref(''), rule = ref(''), error = ref(''), loading = ref(false)
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
onMounted(() => { void load(); timer = setInterval(load, 10000) })
onUnmounted(() => { disposed = true; if (timer) clearInterval(timer) })
</script>
<template>
  <div>
    <section class="page-head"><div><h1>设备与场站健康排行</h1><p>{{ rule }} · 计算时间 {{ dateTime(computedAt) }} · 每 10 秒刷新</p></div><el-button :loading="loading" @click="load">刷新</el-button></section>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <p class="health-note">当前规则评分：离线或遥测故障 0 分，在线非故障按最高活动告警取 40 / 70 / 90 / 100 分。未知不参与排名；离线扣分不证明硬件故障。按当前台账归组，同分同名次。</p>
    <section class="surface"><div class="surface-head"><h2>设备排行</h2></div>
      <el-table :data="devices" v-loading="loading" empty-text="暂无评分结果">
        <el-table-column label="名次" width="70"><template #default="{row}">{{ row.rank ?? '—' }}</template></el-table-column>
        <el-table-column label="设备" min-width="150"><template #default="{row}"><router-link :to="`/devices/${row.device_id}`">{{ row.device_code }}</router-link></template></el-table-column>
        <el-table-column label="场站" prop="station_code" width="115" />
        <el-table-column label="分数" width="80"><template #default="{row}">{{ row.score ?? '未知' }}</template></el-table-column>
        <el-table-column label="状态" min-width="115"><template #default="{row}">{{ healthState(row.state) }}</template></el-table-column>
        <el-table-column label="评分依据" min-width="240"><template #default="{row}">{{ healthBasis(row.matched_by) }}<span v-if="row.active_level"> · {{ levelLabels[row.active_level as AlarmLevel] }}</span></template></el-table-column>
        <el-table-column label="采样时间（北京）" min-width="175"><template #default="{row}">{{ dateTime(row.sampled_at_ms) }}</template></el-table-column>
      </el-table>
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
<style scoped>.health-note{color:var(--ink-3);font-size:13px;line-height:1.8;margin:16px 0}.health-stations{margin-top:18px}</style>
