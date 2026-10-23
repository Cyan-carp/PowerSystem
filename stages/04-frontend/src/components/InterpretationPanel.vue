<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, ApiError } from '../lib/api'
import { currentUser } from '../lib/auth'
import { realtime } from '../lib/realtime'
import { dateTime, levelLabels, normalizePoints } from '../lib/format'
import { alarmComparison, fixedNumber } from '../lib/detailPresentation'
import { interpretationLayout, interpretationText } from '../lib/interpretation-layout'
import { operationNotice, requiresEquipmentReview } from '../lib/agent-chat'
import { attentionOrder, reasonLabels, shouldAutoOpen } from '../lib/interpretations'
import { alarmMetricLabel, evidenceSourceLabel, evidenceStatusLabel, evidenceToolLabel, isLegacySimulationMetric, legacySimulationNote, monitorAlertLabel, monitorSeverityLabel, normalAlarmSamplingNote } from '../lib/interpretation-display'
import type { AgentModelStatus, Interpretation, AgentEvidence, Metric } from '../types'
import TelemetryChart from './TelemetryChart.vue'
import SuggestionSources from './SuggestionSources.vue'
import InterpretationBrief from './InterpretationBrief.vue'

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
const evidenceRoot = ref<HTMLElement | null>(null)
const selected = computed(() => items.value.find(i => i.id === selectedID.value))
const legacySimulation = computed(() => isLegacySimulationMetric(selected.value?.alarm?.metric))
const display = computed(() => selected.value ? interpretationLayout(selected.value) : null)
const comparison = computed(() => selected.value?.alarm ? alarmComparison(selected.value.alarm.value, selected.value.alarm.threshold, selected.value.alarm.metric === 'ai_failure_risk') : null)
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
    if (old && !all.some(i => i.id === old.id)) {
      try { all.push(await api.interpretation(old.id)) }
      catch (cause) { if (cause instanceof ApiError && cause.status === 404) selectedID.value = all[0]?.id ?? null; else throw cause }
    }
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
async function openEvidence(ids: string[]): Promise<void> {
  expandedEvidence.value = [...new Set([...expandedEvidence.value, ...ids])]
  await nextTick()
  evidenceRoot.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
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
watch(selectedID, () => { expandedEvidence.value = [] })
onMounted(() => {
  void refresh(); void modelStatus()
  unsubscribe = realtime.subscribe(event => {
    if (['connected', 'interpretation_updated', 'alarm_created', 'alarm_acked', 'alarm_recovered'].includes(event.type)) void refresh()
    if (event.type === 'agent_model_updated') void modelStatus()
  })
  timer = setInterval(() => { void refresh() }, 5000)
})
onUnmounted(() => { alive = false; unsubscribe?.(); if (timer) clearInterval(timer) })
</script>

<template>
  <el-button text class="interpretation-entry" @click="show()">告警解读 <span v-if="items.filter(i => !i.read).length">（{{ items.filter(i => !i.read).length }}）</span></el-button>
  <el-drawer v-model="open" append-to-body title="告警智能解读" size="min(820px, 100vw)" :show-close="true" class="interpretation-drawer">
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
      <section class="interpretation-facts" aria-label="事件与触发数据">
        <h3>事件事实</h3>
        <div class="event-heading"><strong>{{ legacySimulation ? alarmMetricLabel('simulation_scenario') : selected.category === 'prediction_risk' ? 'AI 故障风险' : selected.category === 'platform_monitor' ? '平台监控' : '设备越限' }}</strong><el-tag :type="selected.active ? 'warning' : 'info'" size="small">{{ selected.active ? (selected.alarm?.status === 'acked' ? '已确认，尚未结束' : '活动事件') : legacySimulation ? '演示已结束' : '已恢复' }}</el-tag></div>
        <div class="event-meta"><span>{{ display?.device }}</span><span>{{ levelLabels[selected.level] }}<template v-if="selected.alarm_id"> · 告警 #{{ selected.alarm_id }}</template></span><span>触发于 {{ dateTime(selected.occurred_at) }}</span></div>
        <p v-if="legacySimulation" role="note">{{ legacySimulationNote }}</p>
        <div v-else-if="selected.alarm" class="event-values"><div><span>{{ alarmMetricLabel(selected.alarm.metric) }} · 触发值</span><strong>{{ fixedNumber(selected.alarm.value) }}</strong></div><div><span>触发阈值</span><strong>{{ fixedNumber(selected.alarm.threshold) }}</strong></div><p v-if="comparison">{{ comparison.differenceText }}</p></div>
        <p v-if="selected.alarm && !legacySimulation" role="note" class="event-source-note">{{ normalAlarmSamplingNote }}</p>
        <template v-if="!selected.alarm"><p>{{ monitorAlertLabel(monitorLabels?.alertname) }} · {{ monitorSeverityLabel(monitorLabels?.severity) }}</p><p>{{ interpretationText(monitorAnnotations?.summary) }}</p></template>
      </section>
      <details v-if="selected.result?.facts?.length" class="interpretation-raw"><summary>后端规范化事实（非模型推断）</summary><dl><template v-for="fact in selected.result.facts" :key="fact.fact_id"><dt>{{ fact.label }}</dt><dd>{{ fact.display_value }}{{ fact.unit }}</dd></template></dl></details>
      <h3>模型分析</h3>
      <p class="interpretation-warning" v-if="selected.task_status === 'pending' || selected.task_status === 'running'">{{ selected.task_status === 'running' ? '正在生成解读' : '等待解读或有限重试' }}；原告警可正常确认。</p>
      <div v-if="selected.result?.status === 'answered'">
        <InterpretationBrief :item="selected" @open-evidence="openEvidence" />
        <section v-if="selected.result.hypotheses?.length"><h3>候选原因（未证实）</h3><SuggestionSources v-for="(claim, index) in selected.result.hypotheses" :key="index" :claim="claim" :evidence="selected.evidence || []" interpretation-mode /></section>
        <h3>人工检查建议</h3>
        <p v-if="selected.result.suggestions.some(requiresEquipmentReview)" class="suggestions-notice" role="note"><strong>操作说明</strong>{{ operationNotice }}</p>
        <SuggestionSources v-for="(claim, index) in selected.result.suggestions" :key="index" :claim="claim" :evidence="selected.evidence || []" :order="index + 1" interpretation-mode />
      </div>
      <p v-else-if="selected.task_status === 'degraded'" role="status" class="interpretation-warning">模型分析未完成；以上事件事实仍可核查。{{ reasonLabels[selected.reason] || '请人工核查原始事件。' }}</p>
      <aside v-if="selected.result?.limitations?.length" class="interpretation-limit" aria-label="解读适用边界"><strong>解读边界</strong><p v-for="note in selected.result.limitations" :key="note">{{ interpretationText(note) }}</p></aside>
      <section ref="evidenceRoot" class="evidence-section" aria-label="事件证据快照">
      <h3>事件证据快照</h3>
      <el-collapse v-model="expandedEvidence">
        <el-collapse-item v-for="evidence in selected.evidence || []" :key="evidence.id" :title="`${evidence.id} · ${evidenceSourceLabel(evidence)}`" :name="evidence.id">
          <template #title><div class="evidence-heading"><span class="evidence-number">{{ evidence.id }}</span><strong>{{ evidenceSourceLabel(evidence) }}</strong><span class="evidence-status" :class="{ 'evidence-status-warning': evidence.status !== 'ok' }">{{ evidenceStatusLabel(evidence.status) }}</span></div></template>
          <dl class="evidence-metadata"><div><dt>证据类型</dt><dd>{{ evidenceToolLabel(evidence.tool) }}</dd></div><div><dt>数据时间</dt><dd>{{ evidence.data_time ? dateTime(evidence.data_time) : '未提供' }}</dd></div><div><dt>取证时间</dt><dd>{{ dateTime(evidence.collected_at) }}</dd></div></dl>
          <details class="interpretation-raw"><summary>查看原始来源路径</summary><code>{{ evidence.source }}</code></details>
          <template v-if="evidence.tool === 'get_telemetry' && evidence.status === 'ok'">
            <p>原始 {{ evidence.data.original_points }} 点；{{ evidence.data.compressed ? '已压缩至最多 100 个实际点' : '未压缩' }}。</p>
            <TelemetryChart v-if="selected.result?.status !== 'answered'" :points="curvePoints(evidence)" :metric="curveMetric(evidence)" height="240px" />
            <p v-else>曲线与统计已在上方“触发前走势”区域分开展示；本处保留原始快照与来源。</p>
          </template>
          <details class="interpretation-raw"><summary>查看原始数据</summary><pre>{{ JSON.stringify(evidence.data, null, 2) }}</pre></details>
          <router-link v-if="evidence.tool === 'get_telemetry' && selected.alarm" :to="`/devices/${selected.alarm.device_id}`">查看设备与当前曲线</router-link>
          <router-link v-if="evidence.tool === 'get_prediction'" to="/predictions">查看当前预测</router-link>
          <router-link v-if="evidence.tool === 'list_alarms' || evidence.tool === 'get_alarm_detail'" :to="{ path: '/alarms', query: selected.alarm_id ? { id: String(selected.alarm_id) } : {} }">查看告警记录</router-link>
        </el-collapse-item>
      </el-collapse>
      <small>快照用于复核触发时事实，关联页面展示当前数据。</small>
      </section>
    </div>
    <el-empty v-else description="暂无待查看的解读事件" />
    <template #footer>
      <div v-if="selected" class="interpretation-actions">
        <el-button v-if="selected.alarm_id && selected.active && selected.alarm?.status === 'unhandled'" type="primary" :loading="busy" @click="confirm">确认告警并标记已读</el-button>
        <el-button v-else :disabled="selected.read" :loading="busy" @click="confirm">{{ selected.read ? '已阅读' : '标记已读' }}</el-button>
        <el-button @click="open = false">关闭</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.interpretation-picker { width: 100%; margin: 12px 0; }
.interpretation-content { color: var(--ink, #101828); }
.interpretation-facts { padding: 12px 14px; border: 1px solid var(--line); border-left: 3px solid var(--warning-dot); background: var(--card-2); border-radius: var(--r-sm); }
.interpretation-facts strong, .interpretation-facts span { display: block; margin-bottom: 8px; }
.interpretation-facts strong { color: var(--ink); }
.interpretation-facts span { font-size: 12px; color: var(--ink-2); }
.interpretation-facts p { margin: 4px 0 0; font-size: 12px; color: var(--ink-2); }
.interpretation-warning { padding: 10px 12px; border-left: 3px solid var(--warning-dot); background: var(--warning-soft); color: var(--warning); font-size: 12px; }
.interpretation-model { display: grid; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--line-soft); font-size: 12px; color: var(--ink-2); }
.interpretation-content h3 { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 20px 0 10px; color: var(--ink); }
.interpretation-content h3::before { content: ""; width: 6px; height: 6px; background: var(--brand); }
.interpretation-content pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; max-height: 280px; overflow: auto; background: var(--hover); border: 1px solid var(--line-soft); padding: 12px; }
.interpretation-raw { margin: 8px 0; font-size: 12px; color: var(--ink-2); }
.interpretation-raw summary { cursor: pointer; color: var(--brand); width: fit-content; }
.interpretation-raw code { display: block; margin-top: 8px; padding: 8px 10px; overflow-wrap: anywhere; background: var(--hover); border: 1px solid var(--line-soft); }
.interpretation-content p { overflow-wrap: anywhere; font-size: 13px; line-height: 1.7; }
.interpretation-limit, small { color: var(--ink-3); font-size: 12px; }
.interpretation-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
.interpretation-content a { color: var(--brand); font-size: 13px; }
.interpretation-facts { padding: 16px 18px; }
.event-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.event-heading > strong { font-size: 18px; margin: 0; }
.event-meta { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; }
.event-meta span { margin: 0; font-size: 12px; color: var(--ink-3); }
.event-values { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; border-top: 1px solid var(--line); padding-top: 14px; margin-top: 14px; }
.event-values span { color: var(--ink-3); font-size: 11px; margin-bottom: 6px; }
.event-values strong { font-size: 25px; font-weight: 600; line-height: 1.2; margin: 0; font-variant-numeric: tabular-nums; }
.event-values > div + div strong { color: var(--ink-2); }
.event-values p { grid-column: 1 / -1; margin: 0; color: var(--ink-2); font-size: 12px; }
.event-source-note { margin: 10px 0 0; padding: 8px 10px; border-left: 3px solid var(--line); background: var(--card-2); color: var(--ink-3); }
.interpretation-limit { border: 1px solid var(--line); background: var(--card-2); padding: 12px 14px; margin-top: 18px; }
.interpretation-limit strong { font-size: 12px; color: var(--ink-2); }
.interpretation-limit p { margin: 6px 0 0; font-size: 12px; }
.evidence-section { scroll-margin-top: 12px; }
.interpretation-content .suggestions-notice { display: flex; align-items: baseline; gap: 8px; margin: 0 0 14px; padding: 10px 12px; border: 1px solid var(--line); background: var(--card-2); color: var(--ink-3); font-size: 12px; font-weight: 400; line-height: 1.7; }
.suggestions-notice strong { flex-shrink: 0; color: var(--ink-2); font-size: 12px; font-weight: 500; }
.evidence-section :deep(.el-collapse) { border: 0; }
.evidence-section :deep(.el-collapse-item) { margin: 10px 0; border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; }
.evidence-section :deep(.el-collapse-item__header) { height: auto; min-height: 50px; padding: 10px 14px; border-bottom: 0; background: var(--card-2); line-height: 1.6; }
.evidence-section :deep(.el-collapse-item__wrap) { background: var(--card); border-bottom: 0; }
.evidence-section :deep(.el-collapse-item__content) { padding: 12px 14px 14px; border-top: 1px solid var(--line); font-size: 12px; color: var(--ink-2); }
.evidence-heading { display: flex; align-items: center; gap: 10px; width: 100%; padding-right: 10px; text-align: left; }
.evidence-number { display: grid; place-items: center; flex: none; min-width: 30px; height: 26px; padding: 0 5px; background: var(--brand-soft); border: 1px solid var(--brand-line); color: var(--brand); font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; }
.evidence-heading strong { color: var(--ink-2); font-size: 12px; font-weight: 500; }
.evidence-status { margin-left: auto; color: var(--ink-3); font-size: 11px; white-space: nowrap; }
.evidence-status-warning { color: var(--warning); }
.evidence-metadata { display: grid; gap: 7px; margin: 0 0 12px; font-size: 12px; }
.evidence-metadata > div { display: grid; grid-template-columns: 60px minmax(0, 1fr); gap: 12px; }
.evidence-metadata dt { color: var(--ink-3); }
.evidence-metadata dd { margin: 0; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.evidence-section .interpretation-raw { font-size: 11px; }
.evidence-section p, .evidence-section a { font-size: 12px; color: var(--ink-2); }
@media(max-width: 540px) { .interpretation-facts { padding: 12px; } .event-meta { gap: 6px 10px; } }
</style>

<style>
.interpretation-drawer .el-drawer__body { padding: 16px 24px 24px; }
.interpretation-drawer .el-drawer__footer { padding: 12px 24px; border-top: 1px solid var(--line); background: var(--card); }
@media(max-width: 540px) { .interpretation-drawer .el-drawer__body { padding: 12px 14px 18px; } .interpretation-drawer .el-drawer__footer { padding: 12px 14px; } }
</style>
