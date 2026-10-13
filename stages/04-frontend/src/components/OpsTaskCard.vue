<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import type { OpsTask } from '../types'
import { api, ApiError } from '../lib/api'
import { terminalTask, taskLabels, pollDelay, canDecideApproval, approvalLabels, attemptLabels, taskErrorText } from '../lib/ops-task'
import { currentUser } from '../lib/auth'
import { renderAnswer } from '../lib/rag-chat'
import BusinessEvidence from './BusinessEvidence.vue'
import InfrastructureEvidence from './InfrastructureEvidence.vue'
import SecurityReviews from './SecurityReviews.vue'
const props = defineProps<{ initial: OpsTask; historical?: boolean }>()
const emit = defineEmits<{ explain: [id: string]; source: [id: string] }>()
const task = ref(props.initial), error = ref(''), cancelling = ref(false)
const deciding = ref(false), decisionError = ref('')
let timer: ReturnType<typeof setTimeout> | undefined, controller: AbortController | undefined, stopped = false
let retryAt = 0
async function update(): Promise<void> {
  if (stopped || document.hidden) return
  const remaining = retryAt - performance.now()
  if (remaining > 0) { clearTimeout(timer); timer = setTimeout(update, remaining); return }
  controller?.abort(); const requestController = new AbortController(); controller = requestController
  let retry = 0
  try { const latest = await api.opsTask(task.value.task_id, requestController.signal); if (stopped || requestController.signal.aborted) return; task.value = latest; error.value = ''; retryAt = 0 }
  catch (e) { if (stopped || requestController.signal.aborted) return; error.value = '状态更新失败，保留上次结果'; if (e instanceof ApiError) retry = e.retryAfter; retryAt = Math.max(retryAt, performance.now() + retry * 1000) }
  const delay = pollDelay(task.value.status, document.hidden, retry)
  clearTimeout(timer)
  if (delay !== null && !stopped) timer = setTimeout(update, delay)
}
function visibility(): void { clearTimeout(timer); controller?.abort(); if (!document.hidden && !terminalTask(task.value.status)) void update() }
async function cancel(): Promise<void> {
  cancelling.value = true
  try { task.value = await api.cancelOpsTask(task.value.task_id); error.value = ''; clearTimeout(timer); if (!terminalTask(task.value.status)) void update() }
  catch (e) { error.value = e instanceof Error ? e.message : '取消失败' }
  finally { cancelling.value = false }
}
async function decide(id: string, decision: 'approve' | 'reject'): Promise<void> {
  if (deciding.value) return
  deciding.value = true; decisionError.value = ''
  try { task.value = await api.decideOpsApproval(task.value.task_id, id, decision); clearTimeout(timer); if (!terminalTask(task.value.status)) void update() }
  catch { decisionError.value = '审批提交未确认，请查看最新任务状态；不会自动重新提交批准。'; void update() }
  finally { deciding.value = false }
}
onMounted(() => { document.addEventListener('visibilitychange', visibility); void update() })
onUnmounted(() => { stopped = true; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility) })
</script>

<template>
  <section class="ops-task" :data-task-id="task.task_id">
    <header><strong>运维任务</strong><span>{{ taskLabels[task.status] }}</span></header>
    <p class="ops-muted">{{ task.intent }}</p>
    <p v-if="error" class="ops-error" role="alert">{{ error }}</p>
    <p v-if="task.error" :class="task.status === 'cancelled' ? 'ops-muted' : 'ops-error'">{{ taskErrorText(task.error) }}</p>
    <p v-if="decisionError" class="ops-error" role="alert">{{ decisionError }}</p>
    <section v-for="approval in task.approvals || []" :key="approval.approval_id" class="ops-approval" :data-approval-id="approval.approval_id">
      <strong>演练容器重启审批 · R2 · {{ approvalLabels[approval.status] }}</strong>
      <p>目标：{{ approval.plan.target }} · 资产版本 {{ approval.plan.snapshot.asset_version }}</p>
      <p>{{ approval.plan.impact }}</p><p class="ops-muted">{{ approval.plan.rollback_limit }}</p>
      <p>审批有效期至 {{ new Date(approval.expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }}</p>
      <details><summary>审批绑定详情</summary><p>容器 ID：{{ approval.plan.snapshot.container_id }}</p><p>镜像：{{ approval.plan.snapshot.image_digest }}</p><p>参数摘要：{{ approval.plan.args_hash }}</p><p>策略：{{ approval.plan.snapshot.policy_version }}</p><p>工具：{{ approval.plan.tool }} · {{ approval.plan.tool_version }}</p></details>
      <div v-if="approval.can_decide === true && canDecideApproval(currentUser?.role, task.status, approval.status, approval.expires_at)">
        <el-button type="primary" size="small" :disabled="deciding" :loading="deciding" @click="decide(approval.approval_id, 'approve')">批准并重启演练容器</el-button>
        <el-button size="small" :disabled="deciding" @click="decide(approval.approval_id, 'reject')">拒绝</el-button>
      </div>
      <p v-if="approval.consumed_at" class="ops-muted">审批已消费，不能重放。</p>
    </section>
    <SecurityReviews :reviews="task.security_reviews || []" :historical="historical || initial.historical || task.historical" />
    <section v-if="task.execution_attempts?.length" class="ops-approval"><strong>程序操作记录</strong>
      <p v-for="attempt in task.execution_attempts" :key="attempt.attempt_id">{{ attemptLabels[attempt.status] }} · {{ attempt.call_id }}</p>
      <p v-if="task.execution_attempts.some(a => a.status === 'execution_uncertain')" class="ops-error">动作结果不确定，禁止自动重试。请另建只读任务核对目标状态。</p>
      <p class="ops-muted">取消不代表已回滚；原审批和已发生操作保留。</p>
    </section>
    <details v-if="task.operation_evidence?.length"><summary>操作前后程序证据</summary><pre>{{ JSON.stringify(task.operation_evidence, null, 2) }}</pre></details>
    <div v-if="task.diagnosis?.summary" class="ops-analysis" v-html="renderAnswer(task.diagnosis.summary)" />
    <p v-if="task.diagnosis?.clarification">{{ task.diagnosis.clarification }}</p>
    <p v-for="(item, index) in task.diagnosis?.hypotheses || []" :key="`h${index}`" class="ops-muted">模型研判 · {{ item }}</p>
    <p v-for="(item, index) in task.diagnosis?.recommendations || []" :key="`r${index}`" class="ops-muted">建议 · {{ item }}</p>
    <BusinessEvidence v-if="task.evidence" :evidence="task.evidence" :historical="historical || initial.historical || task.historical" />
    <InfrastructureEvidence v-if="task.infrastructure_evidence?.length" :evidence="task.infrastructure_evidence" :historical="historical || initial.historical || task.historical" />
    <details v-if="task.steps?.length"><summary>程序执行步骤 · {{ task.steps.length }}</summary><div v-for="step in task.steps" :key="step.step_no"><p>{{ step.step_no }} · {{ step.tool }} · {{ step.status }}</p><pre v-if="step.result">{{ JSON.stringify(step.result, null, 2) }}</pre></div></details>
    <details v-if="task.sources?.length"><summary>任务来源 · {{ task.sources.length }}</summary><p v-for="item in task.sources" :key="item.id">{{ item.title }} · {{ item.collected_at }}<el-button v-if="item.kind === 'document'" text @click="emit('source', item.id)">阅读章节</el-button></p></details>
    <p v-for="warning in task.warnings || []" :key="warning" class="ops-muted">{{ warning }}</p>
    <footer><small>{{ task.task_id }}</small><el-button v-if="!terminalTask(task.status)" size="small" :loading="cancelling" @click="cancel">取消任务</el-button><el-button v-else size="small" @click="emit('explain', task.task_id)">解释此结果</el-button></footer>
  </section>
</template>

<style scoped>
.ops-task{border:1px solid var(--brand-line);background:var(--card);border-radius:14px;padding:16px;margin-top:16px}.ops-task header,.ops-task footer{display:flex;justify-content:space-between;gap:12px;align-items:center}.ops-task header span{font-size:12px;color:var(--brand)}.ops-muted,.ops-task small{font-size:12px;color:var(--ink-3);overflow-wrap:anywhere}.ops-error{color:var(--danger);font-size:13px}.ops-task pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto;background:var(--bg);padding:12px;border-radius:8px}.ops-task footer{margin-top:14px;flex-wrap:wrap}.ops-analysis{line-height:1.8;overflow-wrap:anywhere}
.ops-approval{border:1px solid var(--brand-line);border-radius:10px;padding:12px;margin:12px 0;background:var(--brand-soft);overflow-wrap:anywhere}.ops-approval p{font-size:13px;line-height:1.6}
</style>
