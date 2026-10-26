<script setup lang="ts">
import { computed, nextTick, ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElForm, ElFormItem, ElInputNumber, ElMessageBox } from 'element-plus'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'
import { token } from '../lib/auth'
import { getRun, cancelRun, consumeRun, type RunReceipt, type RunSnapshot } from '../lib/agent-runs'
import { listSchedules, scheduleHistory, proposeSchedule, controlSchedule, previewRule, defaultConfig, type Schedule, type HistoryItem } from '../lib/agent-schedules'
import { dateTime } from '../lib/format'
import RunAnswer from '../components/RunAnswer.vue'
const route = useRoute(), router = useRouter()
const schedules = ref<Schedule[]>([]), recent = ref<(RunReceipt & { capability: string })[]>([]), history = ref<HistoryItem[]>([])
const config = ref(defaultConfig()), editing = ref<Schedule>(), activeTask = ref<Schedule>(), run = ref<RunSnapshot>()
const error = ref(''), busy = ref(false), stopped = ref(false), connection = ref('live')
let controller: AbortController | undefined, generation = 0, disposed = false
const historyOffset = ref(0), historyMore = ref(false)
const preview = ref({ device_id: 801, metric: 'temperature', start: '2026-10-23T00:00:00+08:00', end: '2026-10-23T00:01:00+08:00', operator: 'gt', threshold: 70, baseline_threshold: 75, expected_interval_seconds: 5 })
const labels = { active: '运行中', paused: '已暂停', cancelled: '已取消' }
const scopes = { all: '全平台', business: '业务设备', baseline: '基线设备' }
const capabilityLabels: Record<string, string> = { 'assistant.answer': '对话回答', 'schedule.propose': '配置审查', 'rule.preview': '规则预演' }
const runStatusLabels: Record<string, string> = { queued: '排队中', running: '运行中', awaiting_approval: '待批准', reconciling: '核查中', pending_reconciliation: '待核查', succeeded: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消' }
const capText = (value: string) => capabilityLabels[value] || value
const runStatusText = (value: string) => runStatusLabels[value] || value
/** 概览条：只统计真实存在的状态，不做推测。 */
const activeCount = computed(() => schedules.value.filter(item => item.status === 'active').length)
const pausedCount = computed(() => schedules.value.filter(item => item.status === 'paused').length)
const cancelledCount = computed(() => schedules.value.filter(item => item.status === 'cancelled').length)
const nextRunAt = computed(() => schedules.value
  .filter(item => item.status === 'active' && item.next_run_at)
  .map(item => item.next_run_at as string).sort()[0] || '')
const shortTime = (value: string) => dateTime(value).slice(0, 16)
const compactTime = (value: string) => dateTime(value).slice(5, 16)
const kindLabel = (item: Schedule) => item.config.task_type === 'daily_report' ? '定期日报' : '定期巡检'
const modeText = (item: Schedule) => item.config.mode === 'daily' ? `每日 ${item.config.daily_time || '—'}` : `每 ${item.config.interval_minutes || '—'} 分钟`
const grantText = (item: Schedule) => !item.grant
  ? '无有效授权'
  : item.grant.revoked_at ? `授权已撤销（${dateTime(item.grant.revoked_at)}）` : `授权至 ${dateTime(item.grant.expires_at)}`
const grantWarn = (item: Schedule) => !item.grant || !!item.grant.revoked_at
/** 任务抽屉：与智能助手历史会话同一套「贴边抽屉 + 左缘把手」交互 */
const tasksOpen = ref(sessionStorage.getItem('sched-tasks-open') !== '0')
function toggleTasks(open?: boolean): void {
  tasksOpen.value = open ?? !tasksOpen.value
  try { sessionStorage.setItem('sched-tasks-open', tasksOpen.value ? '1' : '0') } catch { /* 存储不可用时仅本次生效 */ }
}
async function refresh() {
  const identity = token.value
  const value = await listSchedules()
  if (disposed || identity !== token.value) return
  schedules.value = value.list; recent.value = value.runs
  // 抽屉详情持有的是快照对象：刷新后按 id 换成本次拉取的最新状态。
  if (activeTask.value) activeTask.value = schedules.value.find(item => item.id === activeTask.value?.id) || activeTask.value
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
  // 运行面板在主栏顶部，可能被折叠的任务抽屉推出视野。
  await nextTick()
  document.querySelector('.run-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
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
  if (!append) { history.value = []; historyOffset.value = 0 }
  const value = await scheduleHistory(mine, historyOffset.value)
  if (activeTask.value?.id !== mine || token.value !== identity || disposed) return
  history.value = append ? [...history.value, ...value.list] : value.list
  historyOffset.value += value.list.length; historyMore.value = value.list.length === 50
}
async function openTask(item: Schedule) {
  activeTask.value = item
  await expandTasks()
  await loadHistory(item)
}
function backToList() {
  activeTask.value = undefined; history.value = []; historyOffset.value = 0; historyMore.value = false
}
/** 窄屏抽屉在正文下方收起时，点开任务先把它展开 */
async function expandTasks(): Promise<void> {
  if (!tasksOpen.value) toggleTasks(true)
  await nextTick()
}
function closeRun() { run.value = undefined; stopped.value = false; void router.replace({ query: {} }) }
function edit(item: Schedule) {
  editing.value = item; config.value = { ...item.config }
  backToList()
  // 表单在主栏，任务抽屉在右侧：修改配置后必须把表单带回视野。
  document.querySelector('.create-panel')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}
function pauseActive() { const item = activeTask.value; if (item) return safely(() => control(item, 'pause')) }
function resumeActive() { const item = activeTask.value; if (item) return safely(() => control(item, 'resume')) }
function cancelActive() { const item = activeTask.value; if (item) return safely(() => control(item, 'cancel')) }
function editActive() { const item = activeTask.value; if (item) edit(item) }
function moreHistory() { const item = activeTask.value; if (item) return safely(() => loadHistory(item, true)) }
function abandon() { editing.value = undefined; config.value = defaultConfig() }
function stop() { controller?.abort(); stopped.value = true; connection.value = 'stopped' }
function updated(value: RunSnapshot) { run.value = value; void refresh() }
watch(token, () => { generation++; controller?.abort(); schedules.value = []; run.value = undefined; recent.value = []; history.value = []; activeTask.value = undefined })
onMounted(() => { void safely(async () => { await refresh(); if (typeof route.query.run === 'string') await openRun(route.query.run) }) })
onUnmounted(() => { disposed = true; generation++; controller?.abort() })
</script>
<template>
  <main class="schedule-page">
    <section class="page-head">
      <div>
        <h1>定期任务与方案预演</h1>
        <p>日报与巡检结果交付本站本人收件箱 · 创建、修改、恢复须审查及一次批准 · 暂停、取消立即缩减授权 · 产物保留 7 天</p>
      </div>
      <div class="head-side">
        <el-button class="tasks-toggle" size="small" @click="toggleTasks()">{{ tasksOpen ? '收起任务' : `任务列表${schedules.length ? ` (${schedules.length})` : ''}` }}</el-button>
        <router-link to="/agent" class="head-link">返回对话</router-link>
      </div>
    </section>

    <p v-if="error" role="alert" class="error">{{ error }}</p>

    <div class="sched-layout">
      <div class="sched-main">
        <section class="overview" aria-label="定期任务概览">
          <div class="ov-cell">
            <span>生效任务</span>
            <strong>{{ activeCount }}<em>/ {{ schedules.length }}</em></strong>
          </div>
          <div class="ov-cell">
            <span>已暂停</span>
            <strong>{{ pausedCount }}</strong>
          </div>
          <div class="ov-cell">
            <span>已取消</span>
            <strong>{{ cancelledCount }}</strong>
          </div>
          <div class="ov-cell wide">
            <span>下次触发</span>
            <strong class="ov-time">{{ nextRunAt ? shortTime(nextRunAt) : '未排期' }}</strong>
          </div>
        </section>

        <!-- 当前选中的原运行：置顶在主栏，表格类产物需要完整宽度 -->
        <section v-if="run" class="panel run-panel">
          <header class="panel-head">
            <h2>当前选中的原运行</h2>
            <span class="run-id">{{ run.run_id }}</span>
            <div class="panel-tools">
              <router-link :to="{ path: '/agent', query: { session: run.session_id } }">在原会话查看</router-link>
              <el-button text @click="closeRun">收起</el-button>
            </div>
          </header>
          <div class="panel-body">
            <RunAnswer :run="run" :connection="stopped ? 'stopped' : connection" @updated="updated" @stop="stop" @resume="safely(() => openRun(run!.run_id))" @cancel="safely(async () => { updated(await cancelRun(run!.run_id)); await openRun(run!.run_id) })" />
          </div>
        </section>

        <!-- 主栏：新建 / 修改配置 → 预演 → 最近审查 -->
        <section class="panel create-panel">
          <header class="panel-head">
            <h2>{{ editing ? '修改配置' : '新建定期任务' }}</h2>
            <span class="panel-sub">{{ editing ? '重新批准后生效' : '提交后进入审查' }}</span>
          </header>
          <p v-if="editing" class="edit-banner">正在修改任务 {{ editing.id }} · 原版本 {{ editing.config_version }}，批准后按新版本生效。</p>
          <el-form label-position="top" class="create-form" @submit.prevent>
            <p class="form-group">交付内容</p>
            <el-form-item class="span-3" label="任务类型"><el-select v-model="config.task_type"><el-option label="日报 · 上一完整北京时间自然日电量" value="daily_report"/><el-option label="巡检 · 时槽前24小时告警调查清单" value="inspection"/></el-select></el-form-item>
            <el-form-item label="范围"><el-select v-model="config.scope"><el-option label="业务设备" value="business"/><el-option label="全平台" value="all"/><el-option label="基线设备" value="baseline"/></el-select></el-form-item>
            <el-form-item label="场站编码"><el-input v-model="config.station_code" clearable placeholder="全部" aria-label="场站编码（可选，与设备取交集）" @clear="config.station_code = null"/></el-form-item>
            <el-form-item label="设备ID"><el-input-number v-model="config.device_id" controls-position="right" :min="1" :precision="0" placeholder="全部" aria-label="设备ID（可选）"/></el-form-item>

            <p class="form-group">触发与授权</p>
            <el-form-item label="触发方式"><el-select v-model="config.mode"><el-option label="每日固定时刻" value="daily"/><el-option label="固定间隔" value="interval"/></el-select></el-form-item>
            <el-form-item v-if="config.mode === 'daily'" label="每日时刻（HH:mm）"><el-input v-model="config.daily_time" placeholder="09:00"/></el-form-item>
            <el-form-item v-else label="间隔分钟（至少15）"><el-input-number v-model="config.interval_minutes" controls-position="right" :min="15" :max="10080" :precision="0" aria-label="间隔分钟（至少15）"/></el-form-item>
            <el-form-item label="时区"><el-input v-model="config.timezone"/></el-form-item>
            <el-form-item label="授权有效期（天）"><el-input-number v-model="config.grant_days" controls-position="right" :min="1" :max="90" :precision="0" aria-label="授权有效期（天，到期自动暂停）"/></el-form-item>
            <el-form-item class="span-2" label="交付位置"><span class="fixed-value">本站本人收件箱 · 固定不可更改</span></el-form-item>
          </el-form>
          <p class="form-note">错过的时槽跳过并记录，不集中补发；授权到期自动暂停；恢复或改范围重新审查批准。</p>
          <div class="create-actions">
            <el-button type="primary" :loading="busy" @click="safely(save)">{{ editing ? '提交修改审查' : '提交配置审查' }}</el-button>
            <el-button v-if="editing" @click="abandon">放弃编辑</el-button>
          </div>
        </section>

        <details class="panel preview-panel">
          <summary>
            <span class="preview-title">候选规则只读历史预演</span>
            <span class="preview-sub">单设备 · 最多 24 小时 5000 点 · 只比较采样命中</span>
          </summary>
          <div class="preview-body">
            <p class="muted">基准与候选阈值均由本人指定；缺少现场真值，不解释为真实收益或漏报率。</p>
            <el-form label-position="top" class="preview-form" @submit.prevent>
              <el-form-item label="设备ID"><el-input-number v-model="preview.device_id" controls-position="right" :min="1" :precision="0" aria-label="预演设备ID"/></el-form-item>
              <el-form-item label="指标"><el-select v-model="preview.metric"><el-option v-for="m in ['temperature','voltage','current','power']" :key="m" :label="m" :value="m"/></el-select></el-form-item>
              <el-form-item label="开始时间（含时区，含）"><el-input v-model="preview.start"/></el-form-item>
              <el-form-item label="结束时间（含时区，不含）"><el-input v-model="preview.end"/></el-form-item>
              <el-form-item label="比较符"><el-select v-model="preview.operator"><el-option v-for="m in ['gt','gte','lt','lte']" :key="m" :label="m" :value="m"/></el-select></el-form-item>
              <el-form-item label="基准阈值"><el-input-number v-model="preview.baseline_threshold" controls-position="right" :precision="0" aria-label="基准阈值"/></el-form-item>
              <el-form-item label="候选阈值"><el-input-number v-model="preview.threshold" controls-position="right" :precision="0" aria-label="候选阈值"/></el-form-item>
              <el-form-item label="假设采样间隔（秒）"><el-input-number v-model="preview.expected_interval_seconds" controls-position="right" :min="1" :max="86400" :precision="0" aria-label="假设采样间隔（秒）"/></el-form-item>
            </el-form>
            <div class="preview-actions">
              <el-button :loading="busy" @click="safely(async () => { const receipt = await previewRule(preview); await refresh(); await openRun(receipt.run_id) })">创建只读预演</el-button>
            </div>
          </div>
        </details>

        <section class="panel recent-panel">
          <header class="panel-head"><h2>最近的审查与预演</h2></header>
          <p v-if="!recent.length" class="empty tight">暂无运行记录。</p>
          <ul v-else class="recent-list">
            <li v-for="item in recent.slice().reverse()" :key="item.run_id">
              <span class="recent-cap">{{ capText(item.capability) }}</span>
              <span class="recent-state" :class="{ done: item.status === 'succeeded' }">{{ runStatusText(item.status) }}</span>
              <button type="button" class="recent-open" @click="safely(() => openRun(item.run_id))">{{ item.run_id }}</button>
            </li>
          </ul>
        </section>
      </div>

      <!-- 本人任务：贴右边缘的抽屉，约整页四分之一宽；列表 → 详情在同一栏内切换 -->
      <div class="sched-drawer-wrap" :class="{ 'is-hidden': !tasksOpen }">
        <button
          type="button" class="drawer-handle"
          :aria-label="tasksOpen ? '收起任务列表' : '展开任务列表'"
          :title="tasksOpen ? '收起任务列表' : '展开任务列表'"
          :aria-expanded="tasksOpen" aria-controls="sched-task-drawer"
          @click="toggleTasks()"
        ><el-icon><ArrowRight v-if="tasksOpen" /><ArrowLeft v-else /></el-icon></button>
        <aside id="sched-task-drawer" class="sched-drawer" :class="{ 'is-collapsed': !tasksOpen }" aria-label="本人任务">
          <div class="drawer-inner">
            <header class="drawer-head">
              <div class="drawer-title">
                <strong>{{ activeTask ? '任务详情' : '本人任务' }}</strong>
                <span v-if="activeTask" class="drawer-count">v{{ activeTask.config_version }}</span>
                <span v-else class="drawer-count">{{ schedules.length }}</span>
              </div>
              <div class="drawer-tools">
                <el-button v-if="activeTask" text size="small" @click="backToList">返回列表</el-button>
                <el-button v-else text size="small" :loading="busy" @click="safely(refresh)">刷新</el-button>
              </div>
            </header>

            <!-- 列表：一行一个任务，点开进详情 -->
            <div v-if="!activeTask" class="drawer-body">
              <p v-if="!schedules.length" class="drawer-empty">暂无定期任务。提交配置审查并批准后在此显示。</p>
              <ul v-else class="task-list">
                <li v-for="item in schedules" :key="item.id">
                  <button
                    type="button" class="task-row"
                    :class="[`st-${item.status}`, { editing: editing?.id === item.id }]"
                    :aria-label="`${kindLabel(item)} · ${labels[item.status]} · 打开详情`"
                    @click="safely(() => openTask(item))"
                  >
                    <span class="row-title"><i class="row-dot" aria-hidden="true" />{{ kindLabel(item) }}</span>
                    <span class="row-status">{{ labels[item.status] }}</span>
                    <span class="row-meta">{{ modeText(item) }}</span>
                    <span class="row-next">{{ item.next_run_at ? `下次 ${compactTime(item.next_run_at)}` : '未排期' }}</span>
                  </button>
                </li>
              </ul>
            </div>

            <!-- 详情：配置摘要 + 操作 + 历史交付 -->
            <div v-else class="drawer-body detail">
              <div class="detail-top">
                <span class="task-status" :class="activeTask.status"><i aria-hidden="true" />{{ labels[activeTask.status] }}</span>
                <span class="detail-kind">{{ kindLabel(activeTask) }}</span>
              </div>
              <p v-if="activeTask.reason" class="detail-reason">{{ activeTask.reason }}</p>
              <dl class="detail-facts">
                <div><dt>触发</dt><dd>{{ modeText(activeTask) }} · {{ activeTask.config.timezone }}</dd></div>
                <div><dt>范围</dt><dd>{{ scopes[activeTask.config.scope] }}</dd></div>
                <div><dt>场站</dt><dd>{{ activeTask.config.station_code || '全部' }}</dd></div>
                <div><dt>设备</dt><dd>{{ activeTask.config.device_id || '全部' }}</dd></div>
                <div><dt>授权</dt><dd :class="{ warn: grantWarn(activeTask) }">{{ grantText(activeTask) }}</dd></div>
                <div><dt>下次</dt><dd>{{ activeTask.next_run_at ? shortTime(activeTask.next_run_at) : '未排期' }}</dd></div>
              </dl>
              <div v-if="activeTask.status !== 'cancelled'" class="detail-actions">
                <el-button size="small" @click="editActive">修改配置</el-button>
                <el-button v-if="activeTask.status === 'active'" size="small" @click="pauseActive">暂停</el-button>
                <el-button v-if="activeTask.status === 'paused'" size="small" @click="resumeActive">申请恢复</el-button>
                <el-button size="small" type="danger" text @click="cancelActive">取消定期任务</el-button>
              </div>
              <section class="detail-history" aria-label="历史交付">
                <header>
                  <h3>历史交付</h3>
                  <span v-if="history.length">{{ history.length }} 次</span>
                </header>
                <p v-if="!history.length" class="drawer-empty">暂无触发记录。</p>
                <ul v-else class="history-list">
                  <li v-for="record in history" :key="record.scheduled_for">
                    <span class="h-time">{{ shortTime(record.scheduled_for) }}</span>
                    <span class="h-status" :class="{ skipped: record.status === 'skipped' }">{{ record.run_status || record.status }}</span>
                    <button v-if="record.run_id" type="button" class="h-open" @click="safely(() => openRun(record.run_id!))">打开运行</button>
                    <span v-if="record.reason" class="h-reason">{{ record.reason }}（跳过 {{ record.skipped_count }} 槽）</span>
                  </li>
                </ul>
                <el-button v-if="historyMore" size="small" class="h-more" @click="moreHistory">更多历史</el-button>
              </section>
            </div>
            <footer class="drawer-foot">交付进本站本人收件箱 · 产物保留 7 天</footer>
          </div>
        </aside>
      </div>
    </div>
  </main>
</template>
<style scoped>
.schedule-page{max-width:1320px;margin:0 auto;color:var(--ink)}
.head-side{display:flex;align-items:center;gap:14px;padding-bottom:3px}
.head-link{font-size:12px;color:var(--ink-3);white-space:nowrap}
.head-link:hover{color:var(--brand-deep)}
.tasks-toggle{display:none}
.error{margin:0 0 14px;padding:9px 12px;border:1px solid var(--warning-line);border-radius:8px;background:var(--warning-soft);font-size:12.5px;color:var(--warning)}

/* 概览条：与顶部深层入口同一套克制风格，只做读数 */
.overview{display:flex;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.ov-cell{flex:1 1 128px;min-width:0;padding:12px 18px 13px}
.ov-cell+.ov-cell{border-left:1px solid var(--line)}
.ov-cell>span{display:block;font-size:11px;letter-spacing:.4px;color:var(--ink-3)}
.ov-cell strong{display:block;margin-top:4px;font-size:22px;font-weight:600;line-height:1.2;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
.ov-cell em{margin-left:5px;font-style:normal;font-size:11.5px;font-weight:400;color:var(--ink-3)}
.ov-cell.wide{flex:1.5 1 180px}
.ov-time{font-size:15px;letter-spacing:0}

/* 布局：主栏 + 右侧任务抽屉（约整页 1/4 宽） */
.sched-layout{display:flex;align-items:flex-start}
.sched-main{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:14px;padding-right:24px}

.panel{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.panel-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.panel-head h2{margin:0;font-size:13.5px;font-weight:600;letter-spacing:.1px;white-space:nowrap}
.panel-sub{font-size:11.5px;color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.panel-head :deep(.el-button){margin-left:auto;flex:none}
.panel-tools{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:11.5px}
.panel-tools a{color:var(--brand-deep)}
.panel-body{padding:16px}
.run-id{font-size:11px;color:var(--ink-3);padding:1px 6px;border:1px solid var(--line);border-radius:4px;background:var(--card-2)}
.empty{margin:0;padding:22px 16px;font-size:12.5px;color:var(--ink-3)}
.empty.tight{padding:14px 16px}

/* ---------- 任务抽屉：贴右边缘、左缘把手、可收起 ---------- */
.sched-drawer-wrap{position:sticky;top:12px;flex:0 0 auto;display:flex;min-height:0}
.drawer-handle{position:absolute;left:0;top:50%;transform:translate(-100%,-50%);z-index:6;display:grid;place-items:center;width:22px;height:56px;padding:0;border:1px solid var(--line-strong);border-right:0;border-radius:10px 0 0 10px;background:var(--card);color:var(--ink-3);transition:color .15s,background .15s}
.drawer-handle:hover{color:var(--ink);background:var(--hover)}
.drawer-handle:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.drawer-handle :deep(.el-icon){font-size:12px}
.sched-drawer{--drawer-w:330px;display:flex;flex-direction:column;min-height:0;width:var(--drawer-w);max-height:calc(100dvh - 96px);background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;transition:width .22s ease,border-color .22s ease,opacity .18s ease}
.sched-drawer.is-collapsed{width:0;border-color:transparent;opacity:0}
/* 收起动画期间保持内容宽度，避免文字逐帧回流；-2px 让出左右边框 */
.drawer-inner{display:flex;flex-direction:column;min-height:0;width:calc(var(--drawer-w) - 2px);flex:1 1 auto}
.drawer-head{display:flex;align-items:center;gap:8px;flex:none;padding:10px 10px 10px 15px;border-bottom:1px solid var(--line-soft)}
.drawer-title{display:flex;align-items:baseline;gap:6px;min-width:0}
.drawer-title strong{font-size:13px;font-weight:600;letter-spacing:.1px;white-space:nowrap}
.drawer-count{font-size:10.5px;color:var(--ink-3);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.drawer-tools{margin-left:auto;display:flex;align-items:center;gap:2px;flex:none}
.drawer-tools :deep(.el-button){margin:0;font-size:12px;border-radius:8px}
.drawer-body{flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior-y:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--line-strong) transparent;padding:6px 8px 12px}
.drawer-body:focus-visible{outline:1px solid var(--brand-line);outline-offset:-1px}
.drawer-empty{margin:0;padding:16px 8px;font-size:12px;line-height:1.7;color:var(--ink-3)}
.drawer-foot{flex:none;padding:9px 15px;border-top:1px solid var(--line-soft);font-size:10.5px;line-height:1.6;color:var(--ink-3)}

/* 列表行：状态点 + 类型 + 状态，第二行模式与下次触发 */
.task-list{list-style:none;margin:0;padding:0}
.task-row{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"title status" "meta next";gap:4px 10px;width:100%;padding:9px 8px 10px 12px;border:0;border-radius:8px;background:none;text-align:left;font:inherit;color:var(--ink-2);cursor:pointer;transition:background .15s}
.task-row::before{content:'';position:absolute;left:0;top:10px;bottom:10px;width:2px;border-radius:2px;background:transparent}
.task-row:hover{background:var(--hover)}
.task-row:focus-visible{outline:2px solid var(--brand);outline-offset:-2px}
.task-row.editing{background:var(--brand-soft)}
.task-row.editing::before{background:var(--brand)}
.row-title{grid-area:title;display:flex;align-items:center;gap:7px;min-width:0;font-size:13px;font-weight:600;color:var(--ink);letter-spacing:-.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row-dot{flex:none;width:6px;height:6px;border-radius:50%;background:var(--ink-4)}
.st-active .row-dot{background:var(--success)}
.st-paused .row-dot{background:var(--warning)}
.st-cancelled .row-dot{background:var(--ink-4)}
.row-status{grid-area:status;font-size:10.5px;line-height:1.6;white-space:nowrap;color:var(--ink-3)}
.st-active .row-status{color:var(--success)}
.st-paused .row-status{color:var(--warning)}
.row-meta{grid-area:meta;font-size:11.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row-next{grid-area:next;font-size:11px;color:var(--ink-3);font-variant-numeric:tabular-nums;white-space:nowrap}

/* 详情 */
.detail{padding:14px 15px 18px}
.detail-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.detail-kind{font-size:13px;font-weight:600;color:var(--ink)}
.task-status{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:11px;font-weight:500;line-height:1.6}
.task-status i{width:5px;height:5px;border-radius:50%;background:currentColor}
.task-status.active{color:var(--success);background:var(--success-soft);border-color:var(--success-line)}
.task-status.paused{color:var(--warning);background:var(--warning-soft);border-color:var(--warning-line)}
.task-status.cancelled{color:var(--ink-3);background:var(--card-2);border-color:var(--line)}
.detail-reason{margin:10px 0 0;padding:7px 9px;border-left:2px solid var(--warning-line);border-radius:0 6px 6px 0;background:var(--warning-soft);font-size:11.5px;line-height:1.6;color:var(--warning)}
.detail-facts{display:grid;gap:7px;margin:13px 0 0;padding-top:12px;border-top:1px solid var(--line)}
.detail-facts>div{display:grid;grid-template-columns:38px minmax(0,1fr);gap:10px;font-size:11.5px;line-height:1.6}
.detail-facts dt{color:var(--ink-3)}
.detail-facts dd{margin:0;color:var(--ink-2);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.detail-facts dd.warn{color:var(--warning)}
.detail-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;padding-top:13px;border-top:1px solid var(--line)}
.detail-actions :deep(.el-button){margin:0}
.detail-history{margin-top:16px;padding-top:13px;border-top:1px solid var(--line)}
.detail-history>header{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.detail-history h3{margin:0;font-size:12px;font-weight:600;color:var(--ink-2)}
.detail-history>header span{font-size:10.5px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.history-list{list-style:none;margin:6px 0 0;padding:0}
.history-list li{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 8px;padding:7px 0;border-top:1px dashed var(--line);font-size:11px}
.history-list li:first-child{border-top:0}
.h-time{color:var(--ink-2);font-variant-numeric:tabular-nums}
.h-status{padding:0 5px;border:1px solid var(--line);border-radius:3px;color:var(--ink-3);font-size:10px;line-height:1.6}
.h-status.skipped{color:var(--warning);border-color:var(--warning-line);background:var(--warning-soft)}
.h-open{margin-left:auto;padding:0;border:0;background:none;cursor:pointer;font:inherit;font-size:11px;color:var(--brand-deep)}
.h-open:hover{text-decoration:underline}
.h-reason{flex-basis:100%;color:var(--ink-3);line-height:1.6}
.h-more{margin-top:6px}

/* 创建配置：按「交付 / 触发 / 授权」三段排，字段三列 */
.edit-banner{margin:0;padding:9px 16px;border-bottom:1px solid var(--line);background:var(--warning-soft);font-size:11.5px;line-height:1.6;color:var(--warning)}
.create-form{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:end;gap:0 14px;padding:14px 16px 4px}
.create-form :deep(.span-3){grid-column:1/-1}
.create-form :deep(.span-2){grid-column:span 2}
.create-form :deep(.el-form-item){margin-bottom:11px;min-width:0}
.create-form :deep(.el-form-item__label){margin-bottom:3px;font-size:11.5px;font-weight:500;line-height:1.5;color:var(--ink-2)}
.create-form :deep(.el-select),.create-form :deep(.el-input),.create-form :deep(.el-input-number){width:100%}
.form-group{grid-column:1/-1;display:flex;align-items:center;gap:8px;margin:15px 0 9px;font-size:10.5px;font-weight:600;letter-spacing:.8px;color:var(--ink-3)}
.form-group::after{content:'';flex:1;height:1px;background:var(--line)}
.form-group:first-child{margin-top:0}
.fixed-value{display:flex;align-items:center;width:100%;height:32px;padding:0 10px;border:1px dashed var(--line-strong);border-radius:6px;background:var(--card-2);font-size:12px;color:var(--ink-2)}
.form-note{margin:0;padding:10px 16px 0;border-top:1px solid var(--line-soft);font-size:11px;line-height:1.7;color:var(--ink-3)}
.create-actions{display:flex;gap:8px;justify-content:flex-end;padding:12px 16px 14px}
.create-actions :deep(.el-button){margin:0}
.create-actions :deep(.el-button--primary){min-width:150px}

.recent-list{list-style:none;margin:0;padding:0}
.recent-list li{display:flex;align-items:center;gap:8px;padding:9px 16px;border-bottom:1px solid var(--line-soft);font-size:11.5px}
.recent-list li:last-child{border-bottom:0}
.recent-cap{color:var(--ink-2)}
.recent-state{padding:0 6px;border:1px solid var(--line);border-radius:3px;font-size:10.5px;color:var(--warning)}
.recent-state.done{color:var(--success);border-color:var(--success-line);background:var(--success-soft)}
.recent-open{margin-left:auto;max-width:58%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0;border:0;background:none;cursor:pointer;font-size:11px;color:var(--brand-deep)}
.recent-open:hover{text-decoration:underline}

/* 预演：低频工具，默认收起，展开后两列 */
.preview-panel>summary{display:flex;align-items:baseline;gap:10px;padding:13px 16px;cursor:pointer;list-style:none}
.preview-panel>summary::-webkit-details-marker{display:none}
.preview-panel>summary::after{content:'展开';margin-left:auto;font-size:11.5px;color:var(--ink-3)}
.preview-panel[open]>summary{border-bottom:1px solid var(--line)}
.preview-panel[open]>summary::after{content:'收起'}
.preview-title{font-size:13.5px;font-weight:600}
.preview-sub{font-size:11.5px;color:var(--ink-3)}
.preview-body{padding:14px 16px 16px}
.preview-body .muted{margin:0 0 12px;font-size:11.5px;line-height:1.7;color:var(--ink-3)}
.preview-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}
.preview-form :deep(.el-form-item){margin-bottom:12px}
.preview-form :deep(.el-form-item__label){margin-bottom:4px;font-size:11.5px;font-weight:500;line-height:1.5;color:var(--ink-2)}
.preview-form :deep(.el-select),.preview-form :deep(.el-input),.preview-form :deep(.el-input-number){width:100%}
.preview-actions{display:flex;justify-content:flex-end;border-top:1px solid var(--line-soft);padding-top:12px}
.preview-actions :deep(.el-button){margin:0}

@media(max-width:1180px){
  /* 窄屏：抽屉回到正文内、贴满宽度，收起时只留标题栏的「任务列表」入口 */
  .sched-layout{flex-direction:column}
  .sched-main{order:2;padding-right:0;width:100%}
  .sched-drawer-wrap{order:1;position:static;width:100%;margin-bottom:14px}
  .sched-drawer-wrap.is-hidden{margin-bottom:0}
  .sched-drawer{width:100%;max-height:min(420px,46vh)}
  .sched-drawer.is-collapsed{width:100%;height:0;border-color:transparent;opacity:0;max-height:0}
  .drawer-inner{width:100%}
  .drawer-handle{display:none}
  .tasks-toggle{display:inline-flex}
  /* 通栏时一行放下，避免标题与状态被拉开半屏 */
  .task-row{grid-template-columns:minmax(0,1fr) auto auto auto;grid-template-areas:"title meta status next";align-items:baseline;gap:4px 14px}
  .row-status{white-space:nowrap}
}
@media(max-width:1100px){
  .overview .ov-cell{flex:1 1 46%;border-left:0}
  .ov-cell+.ov-cell{border-left:0;border-top:1px solid var(--line)}
  .ov-cell:nth-child(2){border-left:1px solid var(--line);border-top:0}
}
@media(max-width:760px){
  .preview-form,.create-form{grid-template-columns:1fr}
  .create-form :deep(.span-3),.create-form :deep(.span-2){grid-column:1/-1}
  .head-side{padding-bottom:0}
  .page-head h1{font-size:21px}
  .sched-drawer{max-height:none}
}
</style>
