<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../lib/api'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, levelLabels, statusLabels } from '../lib/format'
import type { Alarm, AlarmLevel, AlarmStatus } from '../types'

const route = useRoute()
const router = useRouter()
const rows = ref<Alarm[]>([])
const selected = ref<Alarm | null>(null)
const drawer = ref(false)
const page = ref(1)
const total = ref(0)
const level = ref<AlarmLevel | ''>('')
const status = ref<AlarmStatus | ''>('')
const loading = ref(false)
const acting = ref(false)
const error = ref('')
const detailError = ref('')
let unsubscribe: (() => void) | null = null
let periodic: ReturnType<typeof setInterval> | null = null

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try { const result = await api.alarms(page.value, 20, status.value, level.value); rows.value = result.list || []; total.value = result.total }
  catch (cause) { error.value = errorMessage(cause) }
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

function filter(): void { page.value = 1; void load() }
watch(() => route.query.id, (id) => { const parsed = Number(id); if (Number.isInteger(parsed) && parsed > 0) void openAlarm(parsed) }, { immediate: true })
onMounted(() => {
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
  <section class="page-head"><div><span class="section-kicker">ALARM MANAGEMENT</span><h1>告警中心</h1><p>按级别、状态查找告警，并完成确认闭环。</p></div><div class="head-stat">筛选结果 <strong>{{ total }}</strong></div></section>
  <section class="surface"><div class="toolbar"><div class="toolbar-title">告警记录 <span>新事件会实时同步</span></div><div class="toolbar-actions"><el-select v-model="level" placeholder="全部级别" style="width: 135px" @change="filter"><el-option label="全部级别" value="" /><el-option v-for="(label, value) in levelLabels" :key="value" :label="label" :value="value" /></el-select><el-select v-model="status" placeholder="全部状态" style="width: 135px" @change="filter"><el-option label="全部状态" value="" /><el-option v-for="(label, value) in statusLabels" :key="value" :label="label" :value="value" /></el-select></div></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
    <el-table v-loading="loading" :data="rows" stripe empty-text="暂无符合条件的告警" class="data-table" @row-click="(row: Alarm) => openAlarm(row.id)"><el-table-column label="级别" width="95"><template #default="{ row }"><span class="severity-label" :class="row.level"><span class="severity-dot" :class="row.level" />{{ levelLabels[row.level as AlarmLevel] }}</span></template></el-table-column><el-table-column label="告警内容" min-width="200"><template #default="{ row }"><strong>{{ row.metric === 'ai_failure_risk' ? 'AI 故障风险' : row.metric + ' 越限' }}</strong></template></el-table-column><el-table-column label="设备 ID" prop="device_id" width="110" /><el-table-column label="触发时间" min-width="175"><template #default="{ row }">{{ dateTime(row.triggered_at) }}</template></el-table-column><el-table-column label="状态" width="115"><template #default="{ row }"><el-tag :type="row.status === 'unhandled' ? 'danger' : row.status === 'acked' ? 'warning' : 'success'" effect="plain">{{ statusLabels[row.status as AlarmStatus] }}</el-tag></template></el-table-column><el-table-column label="操作" width="110"><template #default="{ row }"><el-button link type="primary" @click.stop="openAlarm(row.id)">查看详情 →</el-button></template></el-table-column></el-table>
    <div class="table-footer"><span>共 {{ total }} 条记录</span><el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" /></div>
  </section>
  <el-drawer v-model="drawer" title="告警详情" size="min(440px, 100vw)" @closed="router.replace({ query: {} })">
    <el-alert v-if="detailError" :title="detailError" type="error" show-icon :closable="false" />
    <div v-if="selected" class="drawer-content"><div class="drawer-severity"><span class="severity-dot" :class="selected.level" />{{ levelLabels[selected.level] }}告警 <el-tag :type="selected.status === 'unhandled' ? 'danger' : selected.status === 'acked' ? 'warning' : 'success'">{{ statusLabels[selected.status] }}</el-tag></div><h2>{{ selected.metric === 'ai_failure_risk' ? 'AI 故障风险升高' : selected.metric + ' 指标越限' }}</h2><dl><dt>告警编号</dt><dd>#{{ selected.id }}</dd><dt>设备 ID</dt><dd>{{ selected.device_id }}</dd><dt>实测值</dt><dd>{{ selected.value }}</dd><dt>阈值</dt><dd>{{ selected.threshold }}</dd><dt>触发时间</dt><dd>{{ dateTime(selected.triggered_at) }}</dd><dt>确认时间</dt><dd>{{ dateTime(selected.acked_at) }}</dd><dt>恢复时间</dt><dd>{{ dateTime(selected.recovered_at) }}</dd></dl><div class="drawer-actions"><el-button v-if="selected.status === 'unhandled'" type="primary" :loading="acting" @click="acknowledge(selected)">确认告警</el-button><el-button @click="router.push(`/devices/${selected?.device_id}`)">查看关联设备曲线 →</el-button></div><p class="drawer-note">确认会记录操作人和时间；指标恢复后，本次告警才结束。</p></div>
  </el-drawer>
</template>
