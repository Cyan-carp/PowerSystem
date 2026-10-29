<script setup lang="ts">
/**
 * 单个待批准动作的核对弹窗。
 *
 * 关键点：动作字段是服务端在提议时写下的，可能已经过期、可能已被别人处置，
 * 所以点开先重新读一次快照（inspect），核对绑定、状态、有效期之后才允许决定；
 * 决定提交后再用服务端返回的快照覆盖本地，失败也要回读一次真实状态，绝不本地
 * 猜测「应该批准成功了」。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { decideAction, getRun } from '../lib/agent-runs'
import type { RunAction, RunSnapshot } from '../lib/agent-runs'
import { claimAutoApproval, releaseAutoApproval } from '../lib/approval-auto'
import { authToken } from '../lib/auth'
import { stamp } from '../lib/format'
import Icon from './Icon.vue'

const props = defineProps<{ run: RunSnapshot; actionId: string }>()
const emit = defineEmits<{ updated: [value: RunSnapshot] }>()

const open = ref(false)
const busy = ref(false)
const error = ref('')
const current = ref<RunAction>()
const binding = ref<RunSnapshot>()
const gateKey = computed(() => `${props.run.run_id}:${props.actionId}`)

/** 只有服务端仍列为待批准、且未被标记过期的动作才值得自动弹窗。 */
const stillPending = computed(() =>
  props.run.actions.some((item) => String(item.action_id) === props.actionId && item.status === 'pending' && !item.stale),
)

/** 自动弹窗：待批准动作一出现就自己打开，用户关掉后不重复打扰。 */
function autoOpen(): void {
  if (open.value || busy.value || !stillPending.value) return
  if (!claimAutoApproval(gateKey.value)) return
  void inspect()
}

watch([() => props.actionId, () => props.run.actions, stillPending], autoOpen, { immediate: true })
watch(open, (value) => {
  if (!value) releaseAutoApproval(gateKey.value)
})
onBeforeUnmount(() => releaseAutoApproval(gateKey.value))

/** 定期任务类动作把 config 摊开给人看，比一串 JSON 参数好核对。 */
const SCOPE_CN: Record<string, string> = { all: '全平台', business: '业务设备', baseline: '指定场站' }
const schedule = computed<Record<string, unknown> | undefined>(() => {
  if (!String(current.value?.capability || '').startsWith('schedule.')) return undefined
  let args = current.value?.typed_args as Record<string, unknown> | undefined
  if (!args && typeof current.value?.parameters_summary === 'string') {
    try {
      args = JSON.parse(current.value.parameters_summary) as Record<string, unknown>
    } catch {
      return undefined
    }
  }
  const config = args?.config as Record<string, unknown> | undefined
  return config ? { ...config, expected_version: args?.expected_version } : undefined
})

const text = (value: unknown): string => (typeof value === 'string' ? value : '')

async function inspect(): Promise<void> {
  const identity = authToken.value
  busy.value = true
  error.value = ''
  try {
    const fresh = await getRun(props.run.run_id)
    if (identity !== authToken.value || fresh.session_id !== props.run.session_id || String(fresh.turn_id) !== String(props.run.turn_id)) {
      throw new Error('身份或轮次已变化，请刷新核查')
    }
    emit('updated', fresh)
    const action = fresh.actions.find((item) => item.action_id === props.actionId)
    if (
      !action ||
      action.status !== 'pending' ||
      !action.approval_id ||
      !action.plan_hash ||
      action.stale ||
      Date.parse(action.expires_at || '') <= Date.now()
    ) {
      throw new Error('批准已失效或状态尚待核查，请刷新')
    }
    current.value = action
    binding.value = fresh
    open.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取失败，请刷新核查'
  } finally {
    busy.value = false
    if (!open.value) releaseAutoApproval(gateKey.value)
  }
}

async function decide(decision: 'approve' | 'reject'): Promise<void> {
  if (!current.value || !binding.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const fresh = await decideAction(
      binding.value,
      props.actionId,
      String(current.value.approval_id),
      String(current.value.plan_hash),
      decision,
    )
    emit('updated', fresh)
    open.value = false
    if (fresh.authority_sync_status === 'pending_reconciliation') {
      error.value = '决定尚待权威核查，请刷新；不要重复提议动作'
    }
  } catch (e) {
    error.value = e instanceof Error ? `决定未确认：${e.message}。请刷新核查` : '决定未确认，请刷新核查'
    try {
      emit('updated', await getRun(props.run.run_id))
    } catch {
      /* 保留未确认状态，不假装成功 */
    }
    open.value = false
  } finally {
    busy.value = false
  }
}

const close = () => {
  if (busy.value) return
  open.value = false
}

const onKey = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && open.value) close()
}
document.addEventListener('keydown', onKey)
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <button class="btn btn-sm approve-entry" :disabled="busy" @click="inspect">
    <Icon name="shield" :size="13" />核对并决定
  </button>
  <p v-if="error" class="approve-error" role="alert">{{ error }}</p>

  <Teleport to="body">
    <div v-if="open" class="scrim" @click="close"></div>
    <div v-if="open" class="modal" role="dialog" aria-modal="true" aria-label="核对本次动作">
      <header class="modal-head">
        <span class="modal-icon"><Icon name="shield" :size="17" /></span>
        <div>
          <h2>核对本次动作</h2>
          <p>批准后由服务器重新校验权限、参数与目标版本，再执行一次</p>
        </div>
        <button class="icon-btn modal-close" title="关闭" :disabled="busy" @click="close">
          <Icon name="close" :size="17" />
        </button>
      </header>

      <div class="modal-body">
        <p class="effect">{{ text(current?.effect_summary) || '本次动作未提供效果说明' }}</p>

        <dl class="kv">
          <dt>目标</dt>
          <dd>{{ text(current?.target_summary) || '—' }}<template v-if="current?.target_version"> · 版本 {{ current?.target_version }}</template></dd>
          <dt>风险级别</dt>
          <dd>{{ text(current?.risk_class) || '未标注' }}</dd>
          <dt>有效期</dt>
          <dd class="num">{{ stamp(current?.expires_at) }}</dd>
          <template v-if="current?.policy_version">
            <dt>策略</dt>
            <dd>{{ current?.policy_version }} · 目的地 {{ current?.destination_id }}</dd>
          </template>
          <template v-if="current?.recovery_limit">
            <dt>恢复边界</dt>
            <dd>{{ current?.recovery_limit }}</dd>
          </template>
        </dl>

        <dl v-if="schedule" class="kv schedule-kv">
          <dt>任务</dt>
          <dd>{{ schedule.task_type === 'daily_report' ? '本人日报' : '本人巡检' }}</dd>
          <dt>时区与节奏</dt>
          <dd>
            {{ schedule.timezone }} ·
            {{ schedule.mode === 'daily' ? `每日 ${schedule.daily_time}` : `每 ${schedule.interval_minutes} 分钟` }}
          </dd>
          <dt>范围</dt>
          <dd>
            {{ SCOPE_CN[String(schedule.scope)] || schedule.scope }} · 场站 {{ schedule.station_code || '全部' }} · 设备
            {{ schedule.device_id || '全部' }}
          </dd>
          <dt>原配置版本</dt>
          <dd class="num">{{ schedule.expected_version ?? '—' }}</dd>
        </dl>

        <p v-if="current?.parameters_summary && !schedule" class="params">
          参数：<code>{{ current?.parameters_summary }}</code>
        </p>

        <p class="notice">
          <template v-if="schedule">
            本次配置生效后持续只读授权 {{ schedule.grant_days }} 天，到期暂停、不自动续批；目的地仅本站本人。暂停/取消撤销授权，恢复须重新批准。
          </template>
          <template v-else>
            此决定只用于当前账号、本轮及本次参数。确认告警不代表故障修复；已发生修改不会因取消任务自动撤销。
          </template>
        </p>
      </div>

      <footer class="modal-foot">
        <span class="modal-foot-note">同一批准只能消费一次</span>
        <button class="btn" :disabled="busy" @click="decide('reject')">拒绝</button>
        <button class="btn btn-primary" :disabled="busy" @click="decide('approve')">
          {{ busy ? '提交中…' : '批准本次操作' }}
        </button>
      </footer>
    </div>
  </Teleport>
</template>

<style scoped>
.approve-entry {
  border-color: var(--brand-line);
  color: var(--brand-deep);
  background: var(--brand-soft);
}

.approve-entry:hover:not(:disabled) {
  background: #dbe6fd;
  border-color: #a9c0f6;
  color: var(--brand-deep);
}

.approve-error {
  margin-top: 6px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--bad-deep);
}

.modal {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: min(560px, 94vw);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  background: var(--card);
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-3);
  z-index: 81;
  animation: pop-in 0.18s cubic-bezier(0.22, 0.8, 0.32, 1);
}

@keyframes pop-in {
  from {
    opacity: 0;
    transform: translate(-50%, -48%) scale(0.98);
  }
}

.modal-head {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  padding: 17px 19px 14px;
  border-bottom: 1px solid var(--line);
}

.modal-icon {
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  border-radius: var(--r-md);
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand-deep);
}

.modal-head h2 {
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
}

.modal-head p {
  margin-top: 3px;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--ink-3);
}

.modal-close {
  margin-left: auto;
}

.modal-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 19px 18px;
}

.effect {
  font-size: 13.5px;
  line-height: 1.72;
  color: var(--ink);
  margin-bottom: 14px;
}

.kv {
  margin-bottom: 14px;
}

.kv dt {
  color: var(--ink-3);
  font-size: 12px;
}

.kv dd {
  color: var(--ink-2);
  font-size: 12.5px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.schedule-kv {
  padding-top: 12px;
  border-top: 1px dashed var(--line);
}

.params {
  font-size: 12px;
  color: var(--ink-3);
  line-height: 1.6;
  margin-bottom: 12px;
  overflow-wrap: anywhere;
}

.params code {
  font-size: 11.5px;
  color: var(--ink-2);
}

.notice {
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--ink-3);
}

.modal-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 19px;
  border-top: 1px solid var(--line);
}

.modal-foot-note {
  margin-right: auto;
  font-size: 11px;
  color: var(--ink-4);
}
</style>
