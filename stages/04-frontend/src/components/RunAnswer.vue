<script setup lang="ts">
import { computed, ref } from 'vue'
import { downloadArtifact } from '../lib/agent-artifacts'
import ActionApproval from './ActionApproval.vue'
import type { RunSnapshot } from '../lib/agent-runs'
import type { RAGChatResponse, RAGSource } from '../types'
import { renderAnswer, sourceLink } from '../lib/rag-chat'
import { dateTime } from '../lib/format'
const props = defineProps<{ run: RunSnapshot; response?: RAGChatResponse; connection?: string; error?: string; cancelling?: boolean }>()
defineEmits<{ stop: []; resume: []; cancel: []; source: [source: RAGSource]; updated: [value: RunSnapshot] }>()
const labels: Record<string, string> = { queued: '排队中', running: '运行中', awaiting_approval: '待审批', reconciling: '核查中', pending_reconciliation: '等待权威核查', succeeded: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消' }
const phases: Record<string, string> = { accepted: '已持久受理', queued: '等待执行', starting: '开始执行', started: '开始执行', model: '组织回答', tools: '查询资料与工具', tool: '查询资料与工具', finished: '结果已保存', cancelled: '运行已停止', reconciling: '核查已发生结果', awaiting_approval: '等待审批状态同步', partial: '已有结果已保存', metadata: '状态元数据已保存' }
const terminal = computed(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(props.run.status))
const records = computed(() => props.run.result_refs as Record<string, unknown>[])
const actions = computed(() => props.run.actions as Record<string, unknown>[])
/** 有待批准动作时默认展开审批区，配合自动弹窗给出可核对的上下文。 */
const pendingApproval = computed(() => !terminal.value && actions.value.some(item => item.status === 'pending' && !item.stale))
const artifacts = computed(() => props.run.artifacts as Record<string, unknown>[])
const downloading = ref(''), downloadError = ref('')
function ready(item: Record<string, unknown>): boolean { return item.status === 'ready' && typeof item.expires_at === 'string' && Date.parse(item.expires_at) > Date.now() }
async function download(item: Record<string, unknown>) {
  if (downloading.value) return
  downloading.value = field(item, 'artifact_id'); downloadError.value = ''
  try { await downloadArtifact(downloading.value, props.run.run_id) }
  catch (error) { downloadError.value = error instanceof Error ? error.message : '下载失败，请重试' }
  finally { downloading.value = '' }
}
function field(row: Record<string, unknown>, key: string): string { const value = row[key]; return typeof value === 'string' || typeof value === 'number' ? String(value) : '' }
function state(row: Record<string, unknown>): string { const value = field(row, 'status') || field(row, 'approval_status'); return labels[value] || ({ expired: '已过期', unavailable: '不可用', pending: '待确认', approved: '已批准', rejected: '已拒绝', invalidated: '已失效', executed: '已执行' }[value] || value || '状态未提供') }
</script>

<template>
  <section class="run-answer" :data-run-id="run.run_id" :data-turn-id="run.turn_id" :data-status="run.status">
    <header class="run-head" role="status">
      <span class="run-state" :class="`state-${run.status}`">{{ labels[run.status] || run.status }}</span>
      <span>{{ phases[run.phase] || run.phase || '已受理' }}</span>
      <span v-if="connection === 'reconnecting'">连接中断，正在恢复</span>
      <span v-else-if="connection === 'stopped' && !terminal">已停止接收，后台继续运行</span>
    </header>
    <p v-if="run.progress_message && !terminal" class="run-empty">{{ run.progress_message }}</p>
    <p v-if="run.cancel_requested" class="run-notice">已请求取消。以服务器状态为准；已发生的结果不会自动撤销。</p>
    <p v-if="run.requires_manual_reconciliation" class="run-warning" role="alert">待人工核查：结果尚未完全确认，请勿重复执行。</p>
    <p v-if="error" class="run-warning" role="alert">{{ error }}</p>
    <div v-if="run.final_answer" class="rag-markdown run-primary" v-html="renderAnswer(run.final_answer)" />
    <p v-else class="run-empty">{{ terminal ? '本轮未提供正文，状态与已发生结果见下方。' : '已受理，等待真实进度与回答。' }}</p>
    <p v-if="run.error" class="run-warning">{{ run.error.code === 'deadline_exceeded' ? '运行预算已耗尽' : run.error.message || run.error.code }}</p>
    <details v-if="records.length" class="run-details"><summary>任务结果 · {{ records.length }}</summary>
      <div v-for="(item, i) in records" :key="i" class="run-meta"><span>任务 {{ field(item, 'task_id') || field(item, 'result_ref') }}</span><span>{{ state(item) }}{{ item.historical ? ' · 历史结果' : '' }}</span></div>
    </details>
    <details v-if="response?.sources?.length" class="run-details"><summary>参考来源 · {{ response.sources.length }}</summary>
      <div v-for="item in response.sources" :key="item.url || item.id" class="run-source">
        <strong>{{ item.title }}{{ item.chapter ? ` / ${item.chapter}` : '' }}</strong>
        <small>{{ item.version ? `版本 ${item.version} · ` : '' }}{{ dateTime(item.collected_at) }}</small>
        <el-button v-if="item.kind === 'document'" text size="small" @click="$emit('source', item)">阅读章节</el-button>
        <a v-else-if="item.kind === 'web' && sourceLink(item.url)" :href="sourceLink(item.url)!" target="_blank" rel="noopener noreferrer">打开网页</a>
      </div>
    </details>
    <details v-if="actions.length" class="run-details" :open="pendingApproval"><summary>审批与动作状态 · {{ actions.length }}{{ pendingApproval ? ' · 待批准' : '' }}</summary>
      <div v-for="(item, i) in actions" :key="i" class="run-meta"><span>{{ field(item, 'effect_summary') || field(item, 'capability') || field(item, 'action_id') }}</span><span>{{ state(item) }}{{ item.stale ? ' · 状态待刷新核查' : '' }}</span><small v-if="item.expires_at">有效期 {{ dateTime(field(item, 'expires_at')) }}</small><small v-if="item.observed_at">核对于 {{ dateTime(field(item, 'observed_at')) }}</small></div>
      <div v-for="item in actions" :key="field(item, 'action_id')"><ActionApproval v-if="item.status === 'pending' && !terminal" :run="run" :action-id="field(item, 'action_id')" @updated="$emit('updated', $event)" /><p v-if="item.error_code" class="run-warning">{{ field(item, 'error_code') }}</p><p v-if="item.health_check" class="run-notice">执行后健康检查：{{ field(item, 'health_check') }}</p><p v-if="item.recovery_limit" class="run-notice">恢复边界：{{ field(item, 'recovery_limit') }}</p><pre v-if="item.display_result != null" class="run-notice">{{ field(item, 'capability').startsWith('service.') ? '登记服务观察（仅站内）' : '设备备注' }}：{{ field(item, 'display_result') || '（空）' }}</pre></div>
      <p class="run-empty">批准与执行以服务器当前身份和权威状态为准；结果不明时先对账，不重复操作。</p>
    </details>
    <details v-if="artifacts.length" class="run-details"><summary>产物状态 · {{ artifacts.length }}</summary>
      <div v-for="(item, i) in artifacts" :key="i" class="run-meta"><span>{{ field(item, 'name') || field(item, 'artifact_id') }}</span><span>{{ item.status === 'ready' ? (ready(item) ? '可下载' : '已过期') : state(item) }}</span><small>{{ field(item, 'mime_type') }}{{ item.size != null ? ` · ${field(item, 'size')} 字节` : '' }}{{ item.expires_at ? ` · 到期 ${dateTime(field(item, 'expires_at'))}` : '' }}</small><el-button v-if="ready(item)" size="small" :disabled="!!downloading" :loading="downloading === field(item, 'artifact_id')" @click="download(item)">下载 {{ field(item, 'name') }}</el-button></div>
      <p v-if="downloadError" role="alert" class="run-warning">{{ downloadError }}</p>
    </details>
    <div v-if="!terminal" class="run-controls">
      <el-button v-if="connection !== 'stopped'" text size="small" @click="$emit('stop')">停止接收回答</el-button>
      <el-button v-else text size="small" @click="$emit('resume')">恢复接收</el-button>
      <el-button text size="small" type="danger" :loading="cancelling" @click="$emit('cancel')">请求取消任务</el-button>
    </div>
    <footer class="run-foot">运行 {{ run.run_id }} · 轮次 {{ run.turn_id }}</footer>
  </section>
</template>

<style scoped>
.run-answer{min-width:0;overflow-wrap:anywhere}.run-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--ink-3);font-size:12px;margin:4px 0 14px}.run-state{padding:3px 8px;border:1px solid var(--line);border-radius:6px;color:var(--ink-2)}.state-succeeded{color:var(--success)}.state-failed,.state-partial,.state-reconciling{color:var(--warning)}.run-primary{font-size:14px;line-height:1.85}.run-empty,.run-notice{font-size:12px;color:var(--ink-3);line-height:1.7}.run-warning{font-size:13px;color:var(--warning);line-height:1.7}.run-details{border-top:1px solid var(--line);padding:12px 0;font-size:12px;margin-top:10px}.run-details summary{cursor:pointer;color:var(--ink-2)}.run-meta,.run-source{display:flex;flex-wrap:wrap;gap:7px 16px;margin:12px 0;color:var(--ink-2)}.run-meta small,.run-source small{width:100%;color:var(--ink-3)}.run-source strong{font-weight:500}.run-controls{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.run-controls .el-button+.el-button{margin-left:0}.run-foot{color:var(--ink-3);font-size:10px;margin-top:14px;overflow-wrap:anywhere}.run-answer :deep(pre){overflow-x:auto;max-width:100%}.run-answer :deep(table){display:block;overflow-x:auto;max-width:100%}.run-answer :deep(img){max-width:100%}
</style>
