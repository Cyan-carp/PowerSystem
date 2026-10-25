<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElForm, ElFormItem, ElInputNumber, ElMessageBox } from 'element-plus'
import { token } from '../lib/auth'
import { getRun, cancelRun, consumeRun, type RunReceipt, type RunSnapshot } from '../lib/agent-runs'
import { listSchedules, scheduleHistory, proposeSchedule, controlSchedule, previewRule, defaultConfig, type Schedule, type HistoryItem } from '../lib/agent-schedules'
import { dateTime } from '../lib/format'
import RunAnswer from '../components/RunAnswer.vue'
const route = useRoute(), router = useRouter()
const schedules = ref<Schedule[]>([]), recent = ref<(RunReceipt & { capability: string })[]>([]), history = ref<HistoryItem[]>([])
const config = ref(defaultConfig()), editing = ref<Schedule>(), chosen = ref<Schedule>(), run = ref<RunSnapshot>()
const error = ref(''), busy = ref(false), stopped = ref(false), connection = ref('live')
let controller: AbortController | undefined, generation = 0, disposed = false
const historyOffset = ref(0), historyMore = ref(false)
const preview = ref({ device_id: 801, metric: 'temperature', start: '2026-10-23T00:00:00+08:00', end: '2026-10-23T00:01:00+08:00', operator: 'gt', threshold: 70, baseline_threshold: 75, expected_interval_seconds: 5 })
const labels = { active: '运行中', paused: '已暂停', cancelled: '已取消' }
const scopes = { all: '全平台', business: '业务设备', baseline: '基线设备' }
async function refresh() {
  const identity = token.value
  const value = await listSchedules()
  if (disposed || identity !== token.value) return
  schedules.value = value.list; recent.value = value.runs
}
async function safely(operation: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await operation() } catch (e) { error.value = e instanceof Error ? e.message : '请求未确认，请刷新核查，勿重复创建' }
  finally { busy.value = false }
}
async function openRun(id: string) {
  controller?.abort(); const mine = ++generation, identity = token.value
  const fresh = await getRun(id)
  if (mine !== generation || disposed || identity !== token.value) return
  run.value = fresh; stopped.value = false; connection.value = 'live'
  await router.replace({ query: { run: id } })
  if (['succeeded', 'partial', 'failed', 'cancelled'].includes(fresh.status)) return
  const abort = new AbortController(); controller = abort
  void consumeRun(id, fresh, value => { if (mine === generation && identity === token.value && !disposed) run.value = value }, abort.signal, value => { if (mine === generation) connection.value = value }).then(() => refresh()).catch(e => { if (!abort.signal.aborted && mine === generation) error.value = String(e) })
}
async function save() {
  const value = { ...config.value, daily_time: config.value.mode === 'daily' ? config.value.daily_time : null, interval_minutes: config.value.mode === 'interval' ? (config.value.interval_minutes || 15) : null }
  const receipt = await proposeSchedule(value, editing.value)
  editing.value = undefined; config.value = defaultConfig(); await refresh(); await openRun(receipt.run_id)
}
async function control(item: Schedule, action: 'pause' | 'cancel' | 'resume') {
  if (action === 'cancel') await ElMessageBox.confirm('取消后不能恢复；历史运行与产物会保留。', '取消本人定期任务', { type: 'warning' })
  const result = await controlSchedule(item, action); await refresh()
  if ('run_id' in result) await openRun(result.run_id)
}
async function loadHistory(item: Schedule, append = false) {
  const mine = item.id, identity = token.value
  if (!append) { chosen.value = item; history.value = []; historyOffset.value = 0 }
  const value = await scheduleHistory(mine, historyOffset.value)
  if (chosen.value?.id !== mine || identity !== token.value || disposed) return
  history.value = append ? [...history.value, ...value.list] : value.list
  historyOffset.value += value.list.length; historyMore.value = value.list.length === 50
}
function stop() { controller?.abort(); stopped.value = true; connection.value = 'stopped' }
function updated(value: RunSnapshot) { run.value = value; void refresh() }
watch(token, () => { generation++; controller?.abort(); schedules.value = []; run.value = undefined; recent.value = []; history.value = [] })
onMounted(() => { void safely(async () => { await refresh(); if (typeof route.query.run === 'string') await openRun(route.query.run) }) })
onUnmounted(() => { disposed = true; generation++; controller?.abort() })
</script>
<template>
  <main class="schedule-page">
    <header class="schedule-title"><div><p>智能助手 / 本人任务</p><h1>定期任务与方案预演</h1></div><router-link to="/agent">返回对话</router-link></header>
    <p class="muted">日报与巡检结果交付本站本人收件箱。创建、修改、恢复须审查及一次批准；暂停、取消立即缩减授权。产物保留7天，历史摘要继续可查。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <section class="schedule-box">
      <h2>{{ editing ? '修改配置（重新批准后生效）' : '创建本人定期任务' }}</h2>
      <el-form label-position="top" class="schedule-form" @submit.prevent>
        <el-form-item label="任务类型"><el-select v-model="config.task_type"><el-option label="日报 · 上一完整北京时间自然日电量" value="daily_report"/><el-option label="巡检 · 时槽前24小时告警调查清单" value="inspection"/></el-select></el-form-item>
        <el-form-item label="范围"><el-select v-model="config.scope"><el-option label="业务设备" value="business"/><el-option label="全平台" value="all"/><el-option label="基线设备" value="baseline"/></el-select></el-form-item>
        <el-form-item label="场站编码（可选，与设备取交集）"><el-input v-model="config.station_code" clearable @clear="config.station_code = null"/></el-form-item>
        <el-form-item label="设备ID（可选）"><el-input-number v-model="config.device_id" :min="1" :precision="0" aria-label="设备ID（可选）"/></el-form-item>
        <el-form-item label="时区"><el-input v-model="config.timezone"/></el-form-item>
        <el-form-item label="触发方式"><el-select v-model="config.mode"><el-option label="每日固定时刻" value="daily"/><el-option label="固定间隔" value="interval"/></el-select></el-form-item>
        <el-form-item v-if="config.mode === 'daily'" label="每日时刻（HH:mm）"><el-input v-model="config.daily_time" placeholder="09:00"/></el-form-item>
        <el-form-item v-else label="间隔分钟（至少15）"><el-input-number v-model="config.interval_minutes" :min="15" :max="10080" :precision="0" aria-label="间隔分钟（至少15）"/></el-form-item>
        <el-form-item label="授权有效期（天，到期自动暂停）"><el-input-number v-model="config.grant_days" :min="1" :max="90" :precision="0" aria-label="授权有效期（天，到期自动暂停）"/></el-form-item>
        <el-form-item label="交付位置"><span class="muted">本站本人收件箱（固定，不可更改）</span></el-form-item>
      </el-form>
      <p class="muted">错过的时槽跳过并记录，不集中补发；恢复或改范围重新审查批准。</p>
      <el-button type="primary" :loading="busy" @click="safely(save)">提交配置审查</el-button>
      <el-button v-if="editing" @click="editing = undefined; config = defaultConfig()">放弃编辑</el-button>
    </section>
    <section class="schedule-box">
      <header class="section-head"><h2>本人任务与站内收件箱</h2><el-button :loading="busy" @click="safely(refresh)">刷新任务</el-button></header>
      <p v-if="!schedules.length" class="muted">暂无已生效任务。待批准配置可在下方最近运行中打开。</p>
      <article v-for="item in schedules" :key="item.id" class="schedule-row">
        <strong>{{ item.config.task_type === 'daily_report' ? '定期日报' : '定期巡检' }} · {{ labels[item.status] }} · 版本 {{ item.config_version }}</strong>
        <p>{{ item.config.timezone }} · {{ item.config.mode === 'daily' ? item.config.daily_time : `${item.config.interval_minutes}分钟` }} · {{ scopes[item.config.scope] }} · 场站 {{ item.config.station_code || '全部' }} · 设备 {{ item.config.device_id || '全部' }}</p>
        <p class="muted">交付位置：本人站内收件箱 · 下次：{{ item.next_run_at ? dateTime(item.next_run_at) : '已停止' }} · {{ item.grant ? (item.grant.revoked_at ? `授权已撤销（${dateTime(item.grant.revoked_at)}）` : `授权到期：${dateTime(item.grant.expires_at)}`) : '无有效授权' }}{{ item.reason ? ` · ${item.reason}` : '' }}</p>
        <div class="row-actions"><el-button size="small" @click="safely(() => loadHistory(item))">查看历史交付</el-button><el-button v-if="item.status !== 'cancelled'" size="small" @click="editing = item; config = { ...item.config }">修改配置</el-button><el-button v-if="item.status === 'active'" size="small" @click="safely(() => control(item, 'pause'))">暂停</el-button><el-button v-if="item.status === 'paused'" size="small" @click="safely(() => control(item, 'resume'))">申请恢复</el-button><el-button v-if="item.status !== 'cancelled'" size="small" type="danger" @click="safely(() => control(item, 'cancel'))">取消定期任务</el-button></div>
      </article>
      <div v-if="chosen"><h3>历史交付 · {{ chosen.id }}</h3><p v-if="!history.length" class="muted">暂无触发记录。</p><div v-for="item in history" :key="item.scheduled_for" class="history-row"><span>{{ dateTime(item.scheduled_for) }} · {{ item.run_status || item.status }}{{ item.reason ? ` · ${item.reason}（跳过${item.skipped_count}槽）` : '' }}</span><el-button v-if="item.run_id" text @click="safely(() => openRun(item.run_id!))">打开原运行与产物</el-button></div><el-button v-if="historyMore" @click="safely(() => loadHistory(chosen!, true))">更多历史</el-button></div>
    </section>
    <section class="schedule-box"><h2>候选规则只读历史预演</h2><p class="muted">单设备、最多24小时5000点。基准与候选阈值均由本人指定；只比较采样命中，缺少现场真值，不解释为真实收益或漏报率。</p>
      <el-form label-position="top" class="schedule-form" @submit.prevent><el-form-item label="设备ID"><el-input-number v-model="preview.device_id" :min="1" :precision="0" aria-label="预演设备ID"/></el-form-item><el-form-item label="指标"><el-select v-model="preview.metric"><el-option v-for="m in ['temperature','voltage','current','power']" :key="m" :label="m" :value="m"/></el-select></el-form-item><el-form-item label="开始时间（含时区，含）"><el-input v-model="preview.start"/></el-form-item><el-form-item label="结束时间（含时区，不含）"><el-input v-model="preview.end"/></el-form-item><el-form-item label="比较符"><el-select v-model="preview.operator"><el-option v-for="m in ['gt','gte','lt','lte']" :key="m" :label="m" :value="m"/></el-select></el-form-item><el-form-item label="基准阈值"><el-input-number v-model="preview.baseline_threshold" :precision="0" aria-label="基准阈值"/></el-form-item><el-form-item label="候选阈值"><el-input-number v-model="preview.threshold" :precision="0" aria-label="候选阈值"/></el-form-item><el-form-item label="假设采样间隔（秒）"><el-input-number v-model="preview.expected_interval_seconds" :min="1" :max="86400" :precision="0" aria-label="假设采样间隔（秒）"/></el-form-item></el-form><el-button :loading="busy" @click="safely(async () => { const receipt = await previewRule(preview); await refresh(); await openRun(receipt.run_id) })">创建只读预演</el-button>
    </section>
    <section class="schedule-box"><h2>最近配置与预演运行</h2><div v-for="item in recent" :key="item.run_id" class="history-row"><span>{{ item.capability }} · {{ item.status }} · {{ item.run_id }}</span><el-button text @click="safely(() => openRun(item.run_id))">打开原轮次</el-button></div></section>
    <section v-if="run" class="schedule-box"><h2>当前选中的原运行</h2><RunAnswer :run="run" :connection="stopped ? 'stopped' : connection" @updated="updated" @stop="stop" @resume="safely(() => openRun(run!.run_id))" @cancel="safely(async () => { updated(await cancelRun(run!.run_id)); await openRun(run!.run_id) })"/><router-link :to="{ path: '/agent', query: { session: run.session_id } }">在原会话查看</router-link></section>
  </main>
</template>
<style scoped>
.schedule-page{max-width:1100px;margin:0 auto;padding:24px;color:var(--ink-1)}.schedule-title,.section-head{display:flex;justify-content:space-between;align-items:center;gap:16px}.schedule-title p,.muted{color:var(--ink-3);font-size:13px;line-height:1.8}.schedule-title h1{font-size:24px;font-weight:550;margin:8px 0}.schedule-box{border:1px solid var(--line);border-radius:8px;padding:22px;margin-top:22px;min-width:0}.schedule-box h2{font-size:16px;font-weight:550;margin:0 0 20px}.schedule-form{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 20px}.schedule-form :deep(.el-select),.schedule-form :deep(.el-input-number){width:100%}.schedule-row{border-top:1px solid var(--line);padding:18px 0;font-size:13px;overflow-wrap:anywhere}.row-actions{display:flex;gap:8px;flex-wrap:wrap}.row-actions :deep(.el-button){margin:0}.history-row{display:flex;gap:12px;align-items:center;justify-content:space-between;font-size:12px;overflow-wrap:anywhere;padding:9px 0;border-top:1px solid var(--line)}.error{color:var(--warning)}@media(max-width:760px){.schedule-page{padding:14px}.schedule-form{grid-template-columns:1fr}.schedule-box{padding:16px}.schedule-title{align-items:flex-start}.history-row{align-items:flex-start;flex-direction:column}}
</style>
