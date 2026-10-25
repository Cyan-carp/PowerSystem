<script setup lang="ts">
import { computed, ref } from 'vue'
import { getRun, decideAction, type RunSnapshot } from '../lib/agent-runs'
import { token } from '../lib/auth'
import { dateTime } from '../lib/format'
const props = defineProps<{ run: RunSnapshot; actionId: string }>()
const emit = defineEmits<{ updated: [value: RunSnapshot] }>()
const open = ref(false), busy = ref(false), error = ref('')
const current = ref<Record<string, unknown>>()
const binding = ref<RunSnapshot>()
const schedule = computed<Record<string, unknown> | undefined>(() => {
  if (!String(current.value?.capability || '').startsWith('schedule.')) return undefined
  let args = current.value?.typed_args as Record<string, unknown> | undefined
  if (!args && typeof current.value?.parameters_summary === 'string') {
    try { args = JSON.parse(current.value.parameters_summary) as Record<string, unknown> } catch { return undefined }
  }
  const config = args?.config as Record<string, unknown> | undefined
  return config ? { ...config, expected_version: args?.expected_version } : undefined
})
async function inspect() {
  const identity = token.value
  busy.value = true; error.value = ''
  try {
    const fresh = await getRun(props.run.run_id)
    if (identity !== token.value || fresh.session_id !== props.run.session_id || String(fresh.turn_id) !== String(props.run.turn_id)) throw new Error('身份或轮次已变化，请刷新核查')
    emit('updated', fresh)
    const action = fresh.actions.find(a => a.action_id === props.actionId)
    if (!action || action.status !== 'pending' || !action.approval_id || !action.plan_hash || action.stale || Date.parse(action.expires_at || '') <= Date.now()) throw new Error('批准已失效或状态尚待核查，请刷新')
    current.value = action; binding.value = fresh; open.value = true
  } catch (e) { error.value = e instanceof Error ? e.message : '读取失败，请刷新核查' }
  finally { busy.value = false }
}
async function decide(decision: 'approve' | 'reject') {
  if (!current.value || !binding.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const fresh = await decideAction(binding.value, props.actionId, String(current.value.approval_id), String(current.value.plan_hash), decision)
    emit('updated', fresh); open.value = false
    if ((fresh as unknown as Record<string, unknown>).authority_sync_status === 'pending_reconciliation') error.value = '决定尚待权威核查，请刷新；不要重复提议动作'
  } catch (e) {
    error.value = e instanceof Error ? `决定未确认：${e.message}。请刷新核查` : '决定未确认，请刷新核查'
    try { emit('updated', await getRun(props.run.run_id)) } catch { /* retain unconfirmed state */ }
    open.value = false
  } finally { busy.value = false }
}
</script>
<template>
  <el-button size="small" :loading="busy" @click="inspect">核对并决定</el-button>
  <p v-if="error" role="alert">{{ error }}</p>
  <el-dialog v-model="open" title="核对本次动作" width="min(540px, 94vw)" :close-on-click-modal="false">
    <p>{{ current?.effect_summary }}</p>
    <p>目标：{{ current?.target_summary }} · 版本 {{ current?.target_version }}</p>
    <p v-if="current?.parameters_summary">参数：{{ current?.parameters_summary }}</p>
    <template v-if="schedule"><p>任务：{{ schedule.task_type === 'daily_report' ? '本人日报' : '本人巡检' }} · 时区：{{ schedule.timezone }} · {{ schedule.mode === 'daily' ? schedule.daily_time : `${schedule.interval_minutes}分钟` }}</p><p>范围：{{ schedule.scope }} · 场站 {{ schedule.station_code || '全部' }} · 设备 {{ schedule.device_id || '全部' }} · 原配置版本 {{ schedule.expected_version }}</p><p>持续只读授权：本次配置生效后 {{ schedule.grant_days }} 天，到期暂停，不自动续批；目的地仅本站本人（v1）。暂停/取消撤销授权，恢复须重新批准。</p></template>
    <p>有效期：{{ dateTime(String(current?.expires_at || '')) }}</p>
    <p v-if="current?.policy_version">策略：{{ current?.policy_version }} · 目的地：{{ current?.destination_id }}</p>
    <p v-if="current?.recovery_limit">恢复边界：{{ current?.recovery_limit }}</p>
    <p v-if="schedule">此批准5分钟内仅消费一次，用于本次配置；持续授权不能批准或执行业务修改、运维或外发。停止接收回答不等于暂停定期任务。</p>
    <p v-else>此决定只用于当前账号、本轮及本次参数。确认告警不代表故障修复；已发生修改不会因取消任务自动撤销。</p>
    <p>批准后由服务器重新校验权限、参数与目标版本，再执行一次。</p>
    <template #footer><el-button :disabled="busy" @click="decide('reject')">拒绝</el-button><el-button type="primary" :loading="busy" @click="decide('approve')">批准本次操作</el-button></template>
  </el-dialog>
</template>
