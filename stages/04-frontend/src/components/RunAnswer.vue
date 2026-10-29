<script setup lang="ts">
/**
 * 一次运行（run）的完整呈现：状态、进度、正文、任务结果、审批动作、产物与控制。
 *
 * 这一块替代了原先「等一次性返回再显示一段文本」的对话气泡：
 * - 正文可能还在流式增长（SSE text_delta），所以不能把整卡重建；
 * - 审批动作必须在卡片内部可见并可操作，而不是飘到别处；
 * - 终态之后仍保留运行编号与结果引用，便于事后对账。
 */
import { computed, ref } from 'vue'
import ActionApproval from './ActionApproval.vue'
import Icon from './Icon.vue'
import SourceList from './SourceList.vue'
import { downloadArtifact } from '../lib/agent-artifacts'
import { isTerminalStatus } from '../lib/agent-runs'
import type { RunAction, RunArtifact, RunConnectionState, RunSnapshot } from '../lib/agent-runs'
import type { RAGSource } from '../lib/api'
import { bytes, stamp } from '../lib/format'
import { renderAnswer } from '../lib/task-labels'

const props = defineProps<{
  run: RunSnapshot
  connection?: RunConnectionState
  error?: string
  cancelling?: boolean
  /** 历史轮次里 RAG 服务给的来源列表（运行快照不含来源） */
  sources?: RAGSource[]
  /** 运行正文为空时用会话里存的 answer_markdown 兜底 */
  fallbackAnswer?: string
}>()

const emit = defineEmits<{
  stop: []
  resume: []
  cancel: []
  updated: [value: RunSnapshot]
  source: [item: RAGSource]
}>()

const STATUS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  awaiting_approval: '待批准',
  reconciling: '核查中',
  pending_reconciliation: '待权威核查',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
}

const TONE: Record<string, string> = {
  queued: 'is-plain',
  running: 'is-info',
  awaiting_approval: 'is-warn',
  reconciling: 'is-warn',
  pending_reconciliation: 'is-warn',
  succeeded: 'is-ok',
  partial: 'is-warn',
  failed: 'is-bad',
  cancelled: 'is-plain',
}

const PHASE: Record<string, string> = {
  accepted: '已持久受理',
  queued: '等待执行',
  starting: '开始执行',
  started: '开始执行',
  model: '组织回答',
  tools: '查询资料与工具',
  tool: '查询资料与工具',
  finished: '结果已保存',
  cancelled: '运行已停止',
  reconciling: '核查已发生结果',
  awaiting_approval: '等待批准',
  partial: '已有结果已保存',
  metadata: '状态元数据已保存',
}

const ACTION_STATE: Record<string, string> = {
  pending: '待确认',
  approved: '已批准',
  rejected: '已拒绝',
  executed: '已执行',
  expired: '已过期',
  invalidated: '已失效',
  unavailable: '不可用',
}

const terminal = computed(() => isTerminalStatus(props.run.status))
const actions = computed<RunAction[]>(() => props.run.actions || [])
const artifacts = computed<RunArtifact[]>(() => props.run.artifacts || [])
const answer = computed(() => props.run.final_answer || props.fallbackAnswer || '')
/** 有待批准动作时默认展开审批区，配合自动弹窗给出可核对的上下文。 */
const pendingApproval = computed(() => !terminal.value && actions.value.some((item) => item.status === 'pending' && !item.stale))

const downloading = ref('')
const downloadError = ref('')

const text = (value: unknown): string => (typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '')
const state = (row: RunAction): string => ACTION_STATE[text(row.status)] || text(row.status) || '状态未提供'
const ready = (row: RunArtifact): boolean =>
  row.status === 'ready' && typeof row.expires_at === 'string' && Date.parse(row.expires_at) > Date.now()

async function download(row: RunArtifact): Promise<void> {
  const id = text(row.artifact_id)
  if (!id || downloading.value) return
  downloading.value = id
  downloadError.value = ''
  try {
    await downloadArtifact(id, props.run.run_id)
  } catch (error) {
    downloadError.value = error instanceof Error ? error.message : '下载失败，请重试'
  } finally {
    downloading.value = ''
  }
}

</script>

<template>
  <section class="run" :class="`run-${run.status}`">
    <header class="run-head">
      <span class="chip" :class="TONE[run.status] || 'is-plain'"><i></i>{{ STATUS[run.status] || run.status }}</span>
      <span class="run-phase">{{ PHASE[run.phase] || run.phase || '已受理' }}</span>
      <span v-if="connection === 'reconnecting'" class="run-conn">连接中断，正在恢复</span>
      <span v-else-if="connection === 'stopped' && !terminal" class="run-conn">已停止接收，后台继续运行</span>
      <span class="run-id num" :title="`运行 ${run.run_id} · 轮次 ${run.turn_id}`">{{ run.run_id }}</span>
    </header>

    <p v-if="run.progress_message && !terminal" class="run-progress">
      <Icon name="activity" :size="13" />{{ run.progress_message }}
    </p>
    <p v-if="run.cancel_requested" class="run-note">已请求取消。以服务器状态为准，已发生的结果不会自动撤销。</p>
    <p v-if="run.requires_manual_reconciliation" class="run-warn" role="alert">待人工核查：结果尚未完全确认，请勿重复执行。</p>
    <p v-if="error" class="run-warn" role="alert">{{ error }}</p>

    <div v-if="answer" class="run-answer" v-html="renderAnswer(answer)"></div>
    <p v-else class="run-blank">
      {{ terminal ? '本轮未提供正文，状态与已发生结果见下方。' : '已受理，等待真实进度与回答。' }}
    </p>

    <p v-if="run.error" class="run-warn">
      {{ run.error.code === 'deadline_exceeded' ? '运行预算已耗尽' : run.error.message || run.error.code }}
    </p>

    <details v-if="run.result_refs.length" class="run-details">
      <summary>任务结果 · {{ run.result_refs.length }}</summary>
      <div v-for="item in run.result_refs" :key="item.task_id" class="run-row">
        <span class="run-row-main">任务 {{ item.task_id }}</span>
        <span class="run-row-side">{{ item.status }}{{ item.historical ? ' · 历史结果' : '' }}</span>
      </div>
    </details>

    <details v-if="sources && sources.length" class="run-details">
      <summary>参考来源 · {{ sources.length }}</summary>
      <SourceList :sources="sources" @source="emit('source', $event)" />
    </details>

    <details v-if="actions.length" class="run-details" :open="pendingApproval">
      <summary>
        审批与动作 · {{ actions.length }}<span v-if="pendingApproval" class="run-badge">待批准</span>
      </summary>
      <div v-for="item in actions" :key="text(item.action_id)" class="run-action">
        <div class="run-row">
          <span class="run-row-main">{{ text(item.effect_summary) || text(item.capability) || text(item.action_id) }}</span>
          <span class="run-row-side">{{ state(item) }}{{ item.stale ? ' · 状态待刷新核查' : '' }}</span>
        </div>
        <p v-if="item.target_summary" class="run-source-sub">目标 {{ text(item.target_summary) }}</p>
        <p v-if="item.expires_at" class="run-source-sub">有效期 {{ stamp(text(item.expires_at)) }}</p>
        <p v-if="item.observed_at" class="run-source-sub">核对于 {{ stamp(text(item.observed_at)) }}</p>
        <p v-if="item.error_code" class="run-warn">{{ text(item.error_code) }}</p>
        <p v-if="item.health_check" class="run-note">执行后健康检查：{{ text(item.health_check) }}</p>
        <p v-if="item.recovery_limit" class="run-note">恢复边界：{{ text(item.recovery_limit) }}</p>
        <ActionApproval
          v-if="item.status === 'pending' && !terminal"
          :run="run"
          :action-id="text(item.action_id)"
          @updated="emit('updated', $event)"
        />
      </div>
      <p class="run-foot-note">批准与执行以服务器当前身份和权威状态为准；结果不明时先对账，不重复操作。</p>
    </details>

    <details v-if="artifacts.length" class="run-details">
      <summary>产物 · {{ artifacts.length }}</summary>
      <div v-for="item in artifacts" :key="text(item.artifact_id)" class="run-row">
        <span class="run-row-main">{{ text(item.name) || text(item.artifact_id) }}</span>
        <span class="run-row-side">
          {{ item.status === 'ready' ? (ready(item) ? '可下载' : '已过期') : ACTION_STATE[text(item.status)] || text(item.status) || '状态未提供' }}
          <template v-if="typeof item.size === 'number'"> · {{ bytes(item.size) }}</template>
        </span>
        <button
          v-if="ready(item)"
          class="run-link"
          :disabled="!!downloading"
          @click="download(item)"
        >
          <Icon name="download" :size="13" />{{ downloading === text(item.artifact_id) ? '下载中…' : '下载' }}
        </button>
      </div>
      <p v-if="downloadError" class="run-warn" role="alert">{{ downloadError }}</p>
    </details>

    <div v-if="!terminal" class="run-controls">
      <button v-if="connection !== 'stopped'" class="btn btn-sm" @click="emit('stop')">停止接收回答</button>
      <button v-else class="btn btn-sm" @click="emit('resume')">恢复接收</button>
      <button class="btn btn-sm run-cancel" :disabled="cancelling" @click="emit('cancel')">
        {{ cancelling ? '取消中…' : '请求取消任务' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.run {
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--card);
  padding: 13px 15px 14px;
  min-width: 0;
}

.run-head {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  margin-bottom: 9px;
}

.run-phase {
  font-size: 11.5px;
  color: var(--ink-3);
}

.run-conn {
  font-size: 11px;
  color: var(--warn-deep);
  padding: 1px 7px;
  border-radius: var(--r-pill);
  background: var(--warn-soft);
}

.run-id {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--ink-placeholder);
  white-space: nowrap;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
}

.run-progress {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.run-progress svg {
  color: var(--brand);
  flex: 0 0 auto;
}

.run-note {
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink-3);
  margin-bottom: 6px;
}

.run-warn {
  font-size: 12px;
  line-height: 1.65;
  color: var(--bad-deep);
  margin-bottom: 6px;
}

.run-answer {
  font-size: 13px;
  line-height: 1.82;
  color: var(--ink-2);
  overflow-wrap: anywhere;
}

.run-answer :deep(p) {
  margin: 0 0 9px;
}

.run-answer :deep(p:last-child) {
  margin-bottom: 0;
}

.run-answer :deep(h1),
.run-answer :deep(h2),
.run-answer :deep(h3),
.run-answer :deep(h4) {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--ink);
  margin: 15px 0 7px;
}

.run-answer :deep(ul),
.run-answer :deep(ol) {
  padding-left: 20px;
  margin: 8px 0;
}

.run-answer :deep(li) {
  padding: 2px 0;
}

.run-answer :deep(strong) {
  color: var(--ink);
}

.run-answer :deep(code) {
  font-size: 11.5px;
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-sm);
  padding: 1px 4px;
}

.run-answer :deep(pre) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-sm);
  padding: 9px 10px;
  max-height: 300px;
  overflow: auto;
}

.run-answer :deep(table) {
  display: block;
  max-width: 100%;
  overflow: auto;
  border-collapse: collapse;
  font-size: 12px;
}

.run-answer :deep(th),
.run-answer :deep(td) {
  border: 1px solid var(--line);
  padding: 6px 8px;
  text-align: left;
}

.run-answer :deep(a) {
  color: var(--brand);
}

.run-answer :deep(blockquote) {
  margin: 8px 0;
  padding-left: 11px;
  border-left: 2px solid var(--brand-line);
  color: var(--ink-3);
}

.run-blank {
  font-size: 12px;
  line-height: 1.7;
  color: var(--ink-3);
}

.run-details {
  margin-top: 11px;
  padding-top: 10px;
  border-top: 1px solid var(--line-soft);
  font-size: 12px;
}

.run-details summary {
  cursor: pointer;
  color: var(--ink-3);
  font-size: 11.5px;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 7px;
}

.run-details summary::before {
  content: '';
  width: 5px;
  height: 5px;
  border-right: 1.4px solid var(--ink-4);
  border-bottom: 1.4px solid var(--ink-4);
  transform: rotate(-45deg);
  flex: 0 0 5px;
}

.run-details[open] summary::before {
  transform: rotate(45deg);
}

.run-badge {
  padding: 0 6px;
  border-radius: var(--r-pill);
  background: var(--warn-soft);
  color: var(--warn-deep);
  font-size: 10px;
  font-weight: 600;
}

.run-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px dashed var(--line-soft);
  flex-wrap: wrap;
}

.run-row:last-child {
  border-bottom: 0;
}

.run-row-main {
  color: var(--ink-2);
  overflow-wrap: anywhere;
}

.run-row-side {
  margin-left: auto;
  color: var(--ink-3);
  font-size: 11.5px;
  white-space: nowrap;
}

.run-source-sub {
  margin-top: 3px;
  font-size: 11px;
  color: var(--ink-4);
}

.run-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 6px;
  font-size: 11.5px;
  font-weight: 550;
  color: var(--brand);
}

.run-link:hover {
  color: var(--brand-deep);
  text-decoration: underline;
}

.run-action {
  padding: 8px 0;
  border-bottom: 1px dashed var(--line-soft);
}

.run-action:last-of-type {
  border-bottom: 0;
}

.run-controls {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 11px;
  border-top: 1px solid var(--line-soft);
}

.run-cancel {
  color: var(--bad-deep);
  border-color: #f3c9c9;
}

.run-cancel:hover:not(:disabled) {
  background: var(--bad-soft);
  border-color: #edb4b4;
}

.run-foot-note {
  margin-top: 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--ink-placeholder);
}
</style>
