<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { OpsTask } from '../lib/ops-types'
import { api, ApiError } from '../lib/api'
import {
  approvalLabels,
  attemptLabels,
  canDecideApproval,
  pollDelay,
  taskErrorText,
  taskLabels,
  terminalTask,
} from '../lib/ops-task'
import { hasBusinessEvidence } from '../lib/task-presentation'
import { stamp } from '../lib/format'
import TaskReadableText from './TaskReadableText.vue'
import TaskProgramFacts from './TaskProgramFacts.vue'
import InspectionReportBlock from './InspectionReportBlock.vue'
import BusinessQualityBlock from './BusinessQualityBlock.vue'
import BusinessEvidenceBlock from './BusinessEvidenceBlock.vue'
import InfrastructureEvidenceBlock from './InfrastructureEvidenceBlock.vue'
import SecurityReviewsBlock from './SecurityReviewsBlock.vue'

const props = defineProps<{ initial: OpsTask; historical?: boolean }>()
const emit = defineEmits<{ explain: [id: string]; source: [id: string] }>()

const task = ref(props.initial)
const error = ref('')
const cancelling = ref(false)
const deciding = ref(false)
const decisionError = ref('')

const isHistorical = computed(() => props.historical || props.initial.historical || task.value.historical)
const reviewAttention = computed(() =>
  (task.value.security_reviews ?? []).some((review) => review.state !== 'completed' || review.decision !== 'allow'),
)

let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
let stopped = false
let retryAt = 0

async function update(): Promise<void> {
  if (stopped || document.hidden) return
  const remaining = retryAt - performance.now()
  if (remaining > 0) {
    clearTimeout(timer)
    timer = setTimeout(update, remaining)
    return
  }
  controller?.abort()
  const requestController = new AbortController()
  controller = requestController
  let retry = 0
  try {
    const latest = await api.opsTask(task.value.task_id, requestController.signal)
    if (stopped || requestController.signal.aborted) return
    task.value = latest
    error.value = ''
    retryAt = 0
  } catch (caught) {
    if (stopped || requestController.signal.aborted) return
    error.value = '状态更新失败，保留上次结果'
    if (caught instanceof ApiError) retry = (caught as ApiError & { retryAfter?: number }).retryAfter ?? 0
    retryAt = Math.max(retryAt, performance.now() + retry * 1000)
  }
  const delay = pollDelay(task.value.status, document.hidden, retry)
  clearTimeout(timer)
  if (delay !== null && !stopped) timer = setTimeout(update, delay)
}

function visibility(): void {
  clearTimeout(timer)
  controller?.abort()
  if (!document.hidden && !terminalTask(task.value.status)) void update()
}

async function cancel(): Promise<void> {
  cancelling.value = true
  try {
    task.value = await api.cancelOpsTask(task.value.task_id)
    error.value = ''
    clearTimeout(timer)
    if (!terminalTask(task.value.status)) void update()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '取消失败'
  } finally {
    cancelling.value = false
  }
}

async function decide(id: string, decision: 'approve' | 'reject'): Promise<void> {
  if (deciding.value) return
  deciding.value = true
  decisionError.value = ''
  try {
    task.value = await api.decideOpsApproval(task.value.task_id, id, decision)
    clearTimeout(timer)
    if (!terminalTask(task.value.status)) void update()
  } catch {
    decisionError.value = '审批提交未确认，请查看最新任务状态；不会自动重新提交批准。'
    void update()
  } finally {
    deciding.value = false
  }
}

onMounted(() => {
  document.addEventListener('visibilitychange', visibility)
  void update()
})

onUnmounted(() => {
  stopped = true
  clearTimeout(timer)
  controller?.abort()
  document.removeEventListener('visibilitychange', visibility)
})
</script>

<template>
  <section class="ops-task" :data-task-id="task.task_id">
    <header>
      <strong>{{ task.task_kind === 'security_inspection' ? '项目只读巡检' : '运维任务' }}</strong>
      <span>{{ taskLabels[task.status] }}</span>
    </header>

    <p v-if="isHistorical" class="ops-history">历史任务记录 · 结论按原采集时点阅读，打开会话不会重新取证。</p>
    <p v-if="reviewAttention" class="ops-error" role="alert">
      独立安全审查存在否决、待澄清或未完成项，请查看下方安全意见；不能据此继续执行。
    </p>

    <details class="ops-scope">
      <summary>任务范围与执行约束</summary>
      <TaskReadableText :text="task.intent" />
    </details>

    <p v-if="error" class="ops-error" role="alert">{{ error }}</p>
    <p v-if="task.error" :class="task.status === 'cancelled' ? 'ops-muted' : 'ops-error'">{{ taskErrorText(task.error) }}</p>
    <p v-if="decisionError" class="ops-error" role="alert">{{ decisionError }}</p>

    <section v-for="approval in task.approvals || []" :key="approval.approval_id" class="ops-approval" :data-approval-id="approval.approval_id">
      <strong>演练容器重启审批 · R2 · {{ approvalLabels[approval.status] }}</strong>
      <p>目标：{{ approval.plan.target }} · 资产版本 {{ approval.plan.snapshot.asset_version }}</p>
      <p>{{ approval.plan.impact }}</p>
      <p class="ops-muted">{{ approval.plan.rollback_limit }}</p>
      <p>审批有效期至 {{ stamp(approval.expires_at) }}</p>
      <details>
        <summary>审批绑定详情</summary>
        <p>容器 ID：{{ approval.plan.snapshot.container_id }}</p>
        <p>镜像：{{ approval.plan.snapshot.image_digest }}</p>
        <p>参数摘要：{{ approval.plan.args_hash }}</p>
        <p>策略：{{ approval.plan.snapshot.policy_version }}</p>
        <p>工具：{{ approval.plan.tool }} · {{ approval.plan.tool_version }}</p>
      </details>
      <div
        v-if="approval.can_decide === true && canDecideApproval('admin', task.status, approval.status, approval.expires_at)"
        class="ops-approval-actions"
      >
        <button class="btn btn-sm btn-primary" :disabled="deciding" @click="decide(approval.approval_id, 'approve')">
          {{ deciding ? '提交中…' : '批准并重启演练容器' }}
        </button>
        <button class="btn btn-sm" :disabled="deciding" @click="decide(approval.approval_id, 'reject')">拒绝</button>
      </div>
      <p v-if="approval.consumed_at" class="ops-muted">审批已消费，不能重放。</p>
    </section>

    <InspectionReportBlock
      v-if="task.inspection_report"
      :report="task.inspection_report"
      :historical="historical || initial.historical || task.historical"
    />

    <section v-if="task.execution_attempts?.length" class="ops-approval">
      <strong>程序操作记录</strong>
      <p v-for="attempt in task.execution_attempts" :key="attempt.attempt_id">
        {{ attemptLabels[attempt.status] }} · {{ attempt.call_id }}
      </p>
      <p v-if="task.execution_attempts.some((item) => item.status === 'execution_uncertain')" class="ops-error">
        动作结果不确定，禁止自动重试。请另建只读任务核对目标状态。
      </p>
      <p class="ops-muted">取消不代表已回滚；原审批和已发生操作保留。</p>
    </section>

    <details v-if="task.operation_evidence?.length">
      <summary>操作前后程序证据</summary>
      <pre>{{ JSON.stringify(task.operation_evidence, null, 2) }}</pre>
    </details>

    <TaskProgramFacts :task="task" :historical="isHistorical" />
    <BusinessQualityBlock :quality="task.business_quality" :historical="isHistorical" />
    <p v-if="!task.business_quality && hasBusinessEvidence(task.evidence)" class="ops-muted">
      历史业务正文未按新规则校验，保留原文；不能据此认定当前状态。
    </p>

    <section v-if="task.diagnosis" class="ops-diagnosis" :aria-label="task.business_quality ? '程序业务事实' : 'Operation 模型诊断'">
      <h3>
        {{ task.business_quality ? '程序业务结论' : 'Operation 模型诊断' }}
        <span>{{ task.business_quality ? '按实际调用与业务规则生成' : '解释与推测，独立于程序事实' }}</span>
      </h3>
      <p class="ops-muted">
        {{
          task.business_quality
            ? '结论仅覆盖已取得的事实；缺失信息与历史记录不能证明设备当前状态。'
            : '如模型文字与上方工具字段不一致，以程序返回为准；缺失字段、连续健康和长期稳定性均不能由单次读数补推。'
        }}
      </p>
      <section v-if="task.diagnosis.summary" class="ops-conclusion">
        <h4>{{ task.business_quality ? '业务结论' : '诊断结论' }}</h4>
        <TaskReadableText :text="task.diagnosis.summary" />
      </section>
      <section v-if="task.diagnosis.clarification" class="ops-clarification">
        <h4>需要澄清</h4>
        <TaskReadableText :text="task.diagnosis.clarification" />
      </section>
      <div class="ops-opinions">
        <section v-if="task.diagnosis.hypotheses?.length">
          <h4>模型研判 <span>尚未等同证实</span></h4>
          <ol>
            <li v-for="(item, index) in task.diagnosis.hypotheses" :key="index"><TaskReadableText :text="item" /></li>
          </ol>
        </section>
        <section v-if="task.diagnosis.recommendations?.length">
          <h4>建议与下一步</h4>
          <ol>
            <li v-for="(item, index) in task.diagnosis.recommendations" :key="index"><TaskReadableText :text="item" /></li>
          </ol>
          <p class="ops-muted">建议不代表已执行，也不产生新的操作授权。</p>
        </section>
      </div>
      <details class="ops-original">
        <summary>{{ task.business_quality ? '结构化程序结论' : '原始模型输出 · 保留未格式化文本' }}</summary>
        <pre>{{ JSON.stringify(task.diagnosis, null, 2) }}</pre>
      </details>
    </section>

    <SecurityReviewsBlock :reviews="task.security_reviews || []" :historical="isHistorical" />
    <BusinessEvidenceBlock
      v-if="hasBusinessEvidence(task.evidence) && task.evidence"
      :evidence="task.evidence"
      :historical="isHistorical"
    />
    <InfrastructureEvidenceBlock
      v-if="task.infrastructure_evidence?.length"
      :evidence="task.infrastructure_evidence"
      :historical="historical || initial.historical || task.historical"
    />

    <details v-if="task.steps?.length">
      <summary>程序执行步骤 · {{ task.steps.length }}</summary>
      <div v-for="step in task.steps" :key="step.step_no">
        <p>{{ step.step_no }} · {{ step.tool }} · {{ step.status }}</p>
        <pre v-if="step.result">{{ JSON.stringify(step.result, null, 2) }}</pre>
      </div>
    </details>

    <details v-if="task.sources?.length">
      <summary>任务来源 · {{ task.sources.length }}</summary>
      <p v-for="item in task.sources" :key="item.id">
        {{ item.title }} · {{ stamp(item.collected_at) }}
        <button v-if="item.kind === 'document'" class="ops-link" @click="emit('source', item.id)">阅读章节</button>
      </p>
    </details>

    <p v-for="warning in task.warnings || []" :key="warning" class="ops-muted">{{ warning }}</p>

    <footer>
      <small>{{ task.task_id }}</small>
      <button v-if="!terminalTask(task.status)" class="btn btn-sm" :disabled="cancelling" @click="cancel">
        {{ cancelling ? '取消中…' : '取消任务' }}
      </button>
      <button v-else class="btn btn-sm" @click="emit('explain', task.task_id)">解释此结果</button>
    </footer>
  </section>
</template>

<style scoped>
.ops-task {
  border: 1px solid var(--brand-line);
  background: var(--card);
  border-radius: var(--r-md);
  padding: 18px 20px;
  margin-top: 14px;
  font-size: 12.5px;
  color: var(--ink-2);
}

.ops-task > header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
  font-size: 14px;
}

.ops-task > header span {
  font-size: 11.5px;
  color: var(--brand);
}

.ops-history {
  font-size: 11.5px;
  color: var(--warn-deep);
  line-height: 1.7;
  margin: 10px 0;
}

.ops-scope {
  margin: 12px 0;
  color: var(--ink-3);
  font-size: 11.5px;
}

.ops-muted,
.ops-task small {
  font-size: 11.5px;
  color: var(--ink-4);
  overflow-wrap: anywhere;
}

.ops-error {
  color: var(--bad);
  font-size: 12px;
}

.ops-task pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 320px;
  overflow: auto;
  background: var(--card-soft);
  padding: 10px;
  border-radius: var(--r-sm);
  font-size: 11.5px;
}

.ops-approval {
  border: 1px solid var(--brand-line);
  border-radius: var(--r-md);
  padding: 10px 12px;
  margin: 10px 0;
  background: var(--brand-soft);
  overflow-wrap: anywhere;
}

.ops-approval p {
  font-size: 12px;
  line-height: 1.6;
}

.ops-approval-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.ops-diagnosis {
  margin: 20px 0;
}

.ops-diagnosis h3 {
  font-size: 13.5px;
  margin: 0 0 6px;
  color: var(--ink);
}

.ops-diagnosis h3 span,
.ops-diagnosis h4 span {
  font-size: 11px;
  font-weight: 400;
  color: var(--ink-4);
  margin-left: 6px;
}

.ops-diagnosis h4 {
  font-size: 12.5px;
  margin: 0 0 10px;
  color: var(--ink);
}

.ops-conclusion {
  background: var(--card-soft);
  border-left: 3px solid var(--brand);
  padding: 14px 16px;
  margin: 12px 0;
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
}

.ops-clarification {
  border: 1px solid var(--warn);
  padding: 12px 14px;
  margin: 12px 0;
  border-radius: var(--r-sm);
}

.ops-opinions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
  margin: 18px 0;
}

.ops-opinions > section {
  min-width: 0;
}

.ops-opinions ol {
  padding-left: 20px;
  margin: 0;
}

.ops-opinions li {
  padding: 0 0 12px 4px;
}

.ops-opinions li::marker {
  color: var(--brand);
  font-variant-numeric: tabular-nums;
}

.ops-original {
  font-size: 11.5px;
  color: var(--ink-4);
  margin: 10px 0;
}

.ops-task > details:not(.ops-scope) {
  margin-top: 12px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.ops-task summary {
  cursor: pointer;
  line-height: 1.8;
}

.ops-link {
  color: var(--brand);
  text-decoration: underline;
  margin-left: 6px;
  font-size: 11.5px;
}

.ops-task footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

@media (max-width: 1100px) {
  .ops-opinions {
    grid-template-columns: 1fr;
    gap: 8px;
  }
}

@media (max-width: 640px) {
  .ops-task {
    padding: 14px;
  }

  .ops-conclusion {
    padding: 12px;
  }

  .ops-diagnosis h3 span {
    display: block;
    margin: 4px 0 0;
  }
}
</style>
