<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, ApiError } from '../lib/api'
import { currentUser } from '../lib/auth'
import { realtime } from '../lib/realtime'
import { dateTime, levelLabels, normalizePoints } from '../lib/format'
import { attentionOrder, reasonLabels, shouldAutoOpen } from '../lib/interpretations'
import type { AgentModelStatus, Interpretation, AgentEvidence, Metric } from '../types'
import TelemetryChart from './TelemetryChart.vue'
import SuggestionSources from './SuggestionSources.vue'

const route = useRoute()
const items = ref<Interpretation[]>([])
const selectedID = ref<number | null>(null)
const open = ref(false)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const model = ref<AgentModelStatus | null>(null)
const seen = new Set<number>()
const expandedEvidence = ref<string[]>([])
const selected = computed(() => items.value.find(i => i.id === selectedID.value))
const monitorLabels = computed(() => selected.value?.monitor?.labels as Record<string, string> | undefined)
const monitorAnnotations = computed(() => selected.value?.monitor?.annotations as Record<string, string> | undefined)
const admin = computed(() => currentUser.value?.role === 'admin')
let unsubscribe: (() => void) | undefined
let timer: ReturnType<typeof setInterval> | undefined
let alive = true

async function refresh(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    const all: Interpretation[] = []
    let page = 1
    for (;;) {
      const result = await api.interpretations(page, true)
      all.push(...result.list)
      if (all.length >= result.total || !result.list.length) break
      page++
    }
    if (!alive) return
    // Keep the displayed item available even after confirmation removes it from attention.
    const old = selected.value
    if (old && !all.some(i => i.id === old.id)) all.push(await api.interpretation(old.id))
    if (!alive) return
    items.value = attentionOrder(all)
    error.value = ''
    if (route.path === '/dashboard' && !open.value) {
      const next = items.value.find(i => shouldAutoOpen(i, seen))
      if (next) show(next.id)
    }
  } catch (e) {
    if (alive) error.value = e instanceof Error ? e.message : '解读记录暂不可用'
  } finally { loading.value = false }
}

function show(id?: number): void {
  selectedID.value = id ?? items.value[0]?.id ?? null
  if (selectedID.value !== null) seen.add(selectedID.value)
  open.value = true
}
function selectItem(value: unknown): void { seen.add(Number(value)) }
function curvePoints(evidence: AgentEvidence): [number, number][] {
  const points = evidence.data.points
  return Array.isArray(points) ? normalizePoints(points as [number | string, number][]) : []
}
function curveMetric(evidence: AgentEvidence): Metric { return evidence.data.metric as Metric }

async function confirm(): Promise<void> {
  const item = selected.value
  if (!item || busy.value) return
  busy.value = true
  try {
    if (item.alarm_id && item.alarm?.status === 'unhandled' && item.active) await api.ackAlarm(item.alarm_id)
    await api.readInterpretation(item.id)
    const updated = await api.interpretation(item.id)
    items.value = items.value.map(current => current.id === updated.id ? updated : current)
    await refresh()
    ElMessage.success(item.alarm_id ? '告警已确认，解读已读' : '监控解读已标记阅读')
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      ElMessage.info('告警已被确认或恢复，已刷新状态')
      await refresh()
    } else ElMessage.error(e instanceof Error ? e.message : '确认失败')
  } finally { busy.value = false }
}

async function modelStatus(): Promise<void> {
  if (!admin.value) return
  try { model.value = await api.modelStatus() } catch { model.value = { available: false, reason: 'unavailable', message: '无法获取模型状态' } }
}
async function probe(): Promise<void> {
  busy.value = true
  try { await api.modelProbe(); ElMessage.success('模型检测通过，已恢复符合条件的活动事件') }
  catch (e) { ElMessage.error(e instanceof Error ? e.message : '模型检测失败') }
  finally { await modelStatus(); busy.value = false; await refresh() }
}

watch(() => route.path, () => { void refresh() })
onMounted(() => {
  void refresh(); void modelStatus()
  unsubscribe = realtime.subscribe(event => {
    if (['connected', 'interpretation_updated', 'alarm_acked', 'alarm_recovered'].includes(event.type)) void refresh()
    if (event.type === 'agent_model_updated') void modelStatus()
  })
  timer = setInterval(() => { void refresh() }, 5000)
})
onUnmounted(() => { alive = false; unsubscribe?.(); if (timer) clearInterval(timer) })
</script>

<template>
  <el-button text class="interpretation-entry" @click="show()">告警解读 <span v-if="items.filter(i => !i.read).length">（{{ items.filter(i => !i.read).length }}）</span></el-button>
  <el-drawer v-model="open" append-to-body title="告警智能解读" size="580px" :show-close="true">
    <div v-if="error" role="alert" class="interpretation-warning">{{ error }}</div>
    <div v-if="admin" class="interpretation-model">
      <span>{{ model?.message || '模型状态待检测' }}</span>
      <el-button :loading="busy" @click="probe">检测并恢复模型</el-button>
      <small>检测会请求当前配置的模型，每分钟最多一次。</small>
    </div>
    <el-select v-if="items.length" v-model="selectedID" class="interpretation-picker" @change="selectItem">
      <el-option v-for="item in items" :key="item.id" :value="item.id" :label="`${levelLabels[item.level]} · ${item.alarm_id ? '告警 #' + item.alarm_id : '平台监控'} · ${dateTime(item.occurred_at)}`" />
    </el-select>
    <div v-if="selected" class="interpretation-content">
      <div class="interpretation-facts">
        <strong>{{ selected.category === 'prediction_risk' ? 'AI 风险' : selected.category === 'platform_monitor' ? '平台监控' : '设备越限' }}</strong>
        <span>{{ selected.active ? (selected.alarm?.status === 'acked' ? '已确认，尚未恢复' : '活动事件') : '已恢复' }} · {{ levelLabels[selected.level] }}</span>
        <p>触发时间：{{ dateTime(selected.occurred_at) }}</p>
        <p v-if="selected.alarm">设备 #{{ selected.alarm.device_id }} · {{ selected.alarm.metric }} · 原值 {{ selected.alarm.value }} / 阈值 {{ selected.alarm.threshold }}</p>
        <template v-else><p>{{ monitorLabels?.alertname }} · {{ monitorLabels?.severity }}</p><p>{{ monitorAnnotations?.summary }}</p></template>
      </div>
      <p class="interpretation-warning" v-if="selected.task_status === 'pending' || selected.task_status === 'running'">{{ selected.task_status === 'running' ? '正在生成解读' : '等待解读或有限重试' }}；原告警可正常确认。</p>
      <div v-if="selected.result?.status === 'answered'">
        <h3>结论</h3><p>{{ selected.result.conclusion?.text }}</p>
        <el-button link @click="expandedEvidence = selected.result.conclusion?.evidence_ids || []">展开结论证据：{{ selected.result.conclusion?.evidence_ids.join('、') }}</el-button>
        <h3>人工检查建议</h3>
        <SuggestionSources v-for="(claim, index) in selected.result.suggestions" :key="index" :claim="claim" :evidence="selected.evidence || []" />
      </div>
      <p v-else-if="selected.task_status === 'degraded'" role="status" class="interpretation-warning">智能解读暂不可用，原因尚无法判断。{{ reasonLabels[selected.reason] || '请人工核查原始事件。' }}</p>
      <p v-for="note in selected.result?.limitations || []" :key="note" class="interpretation-limit">{{ note }}</p>
      <h3>事件证据快照</h3>
      <el-collapse v-model="expandedEvidence">
        <el-collapse-item v-for="evidence in selected.evidence || []" :key="evidence.id" :title="`${evidence.id} · ${evidence.tool} · ${evidence.status}`" :name="evidence.id">
          <p>来源：{{ evidence.source }}</p><p>数据时间：{{ evidence.data_time ? dateTime(evidence.data_time) : '未提供' }}</p>
          <p>取证时间：{{ dateTime(evidence.collected_at) }}</p>
          <template v-if="evidence.tool === 'get_telemetry' && evidence.status === 'ok'">
            <p>原始 {{ evidence.data.original_points }} 点；{{ evidence.data.compressed ? '已压缩至最多 100 个实际点' : '未压缩' }}。</p>
            <TelemetryChart :points="curvePoints(evidence)" :metric="curveMetric(evidence)" height="220px" />
          </template>
          <pre>{{ JSON.stringify(evidence.data, null, 2) }}</pre>
          <router-link v-if="evidence.tool === 'get_telemetry' && selected.alarm" :to="`/devices/${selected.alarm.device_id}`">查看设备与当前曲线</router-link>
          <router-link v-if="evidence.tool === 'get_prediction'" to="/predictions">查看当前预测</router-link>
          <router-link v-if="evidence.tool === 'list_alarms' || evidence.tool === 'get_alarm_detail'" :to="{ path: '/alarms', query: selected.alarm_id ? { id: String(selected.alarm_id) } : {} }">查看告警记录</router-link>
        </el-collapse-item>
      </el-collapse>
      <small>快照用于复核触发时事实，关联页面展示当前数据。</small>
      <div class="interpretation-actions">
        <el-button v-if="selected.alarm_id && selected.active && selected.alarm?.status === 'unhandled'" type="primary" :loading="busy" @click="confirm">确认告警并标记已读</el-button>
        <el-button v-else :disabled="selected.read" :loading="busy" @click="confirm">{{ selected.read ? '已阅读' : '标记已读' }}</el-button>
        <el-button @click="open = false">关闭</el-button>
      </div>
    </div>
    <el-empty v-else description="暂无待查看的解读事件" />
  </el-drawer>
</template>

<style scoped>
.interpretation-picker { width: 100%; margin: 12px 0; }
.interpretation-content { color: var(--ink, #1a1a1a); }
.interpretation-facts { padding: 12px 14px; border: 1px solid var(--line); border-left: 3px solid var(--warning-dot); background: #fff; }
.interpretation-facts strong, .interpretation-facts span { display: block; margin-bottom: 8px; }
.interpretation-facts strong { color: var(--ink); }
.interpretation-facts span { font-size: 12px; color: var(--ink-2); }
.interpretation-facts p { margin: 4px 0 0; font-size: 12px; color: var(--ink-2); }
.interpretation-warning { padding: 10px 12px; border-left: 3px solid var(--warning-dot); background: var(--warning-soft); color: var(--warning); font-size: 12px; }
.interpretation-model { display: grid; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--line-soft); font-size: 12px; color: var(--ink-2); }
.interpretation-content h3 { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 20px 0 10px; color: var(--ink); }
.interpretation-content h3::before { content: ""; width: 6px; height: 6px; background: var(--brand); }
.interpretation-content pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; max-height: 280px; overflow: auto; background: var(--hover); border: 1px solid var(--line-soft); padding: 12px; }
.interpretation-content p { overflow-wrap: anywhere; font-size: 13px; line-height: 1.7; }
.interpretation-limit, small { color: var(--ink-3); font-size: 12px; }
.interpretation-actions { display: flex; gap: 8px; margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--line-soft); }
.interpretation-content a { color: var(--brand); font-size: 13px; }
</style>
