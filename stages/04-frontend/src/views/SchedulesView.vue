<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import Icon from '../components/Icon.vue'
import Drawer from '../components/Drawer.vue'
import RunAnswer from '../components/RunAnswer.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import type {
  Device,
  RulePreviewInput,
  RulePreviewResult,
  Schedule,
  ScheduleConfig,
  ScheduleHistoryRow,
  ScheduleRunBrief,
  StationRow,
} from '../lib/api'
import { cancelRun, consumeRun, getRun, isTerminalStatus } from '../lib/agent-runs'
import type { RunConnectionState, RunSnapshot } from '../lib/agent-runs'
import { downloadArtifact, readArtifactJson } from '../lib/agent-artifacts'
import { across, clock, METRIC_CN, METRIC_UNIT, n, stamp } from '../lib/format'
import { askConfirm } from '../lib/toast'

const list = ref<Schedule[]>([])
const runs = ref<ScheduleRunBrief[]>([])
const stations = ref<StationRow[]>([])
const loading = ref(true)
const failed = ref('')
const notice = ref('')
let noticeTimer = 0

const detail = ref<Schedule | null>(null)
const history = ref<ScheduleHistoryRow[]>([])
const historyLoading = ref(false)

const formOpen = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const formError = ref('')
const submitting = ref(false)

const blankConfig = (): ScheduleConfig => ({
  task_type: 'daily_report',
  scope: 'all',
  station_code: null,
  device_id: null,
  timezone: 'Asia/Shanghai',
  mode: 'daily',
  daily_time: '09:00',
  interval_minutes: null,
  grant_days: 30,
  destination_id: 'platform_client',
  destination_version: '1',
})
const form = ref<ScheduleConfig>(blankConfig())

const flash = (text: string) => {
  notice.value = text
  window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => (notice.value = ''), 6000)
}

const load = async () => {
  loading.value = true
  try {
    const [board, stationPayload] = await Promise.all([api.schedules(), api.stations()])
    list.value = board.list
    runs.value = board.runs
    stations.value = stationPayload.list
    failed.value = ''
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const STATUS_CN: Record<string, string> = { active: '运行中', paused: '已暂停', cancelled: '已取消' }
const statusTone = (status: string) => (status === 'active' ? 'ok' : status === 'paused' ? 'warn' : 'idle')

const typeName = (config: ScheduleConfig) => (config.task_type === 'inspection' ? '定期巡检' : '定期日报')
const scopeName = (config: ScheduleConfig) => {
  if (config.scope === 'all') return '全部场站'
  if (config.scope === 'business') return '业务场站'
  return `指定范围 · ${config.station_code || '未指定'}`
}
const triggerName = (config: ScheduleConfig) =>
  config.mode === 'interval' ? `每 ${config.interval_minutes ?? '—'} 分钟` : `每日 ${config.daily_time || '—'}`

const strip = computed(() => {
  const active = list.value.filter((item) => item.status === 'active')
  const paused = list.value.filter((item) => item.status === 'paused')
  const cancelled = list.value.filter((item) => item.status === 'cancelled')
  const next = active
    .map((item) => item.next_run_at)
    .filter((value): value is string => !!value)
    .sort()[0]
  return [
    { key: 'active', label: '运行中', color: 'var(--ok)', value: `${active.length}`, note: '按配置自动执行' },
    { key: 'paused', label: '已暂停', color: 'var(--warn)', value: `${paused.length}`, note: '授权到期或本人暂停' },
    { key: 'cancelled', label: '已取消', color: 'var(--ink-4)', value: `${cancelled.length}`, note: '保留历史，不再执行' },
    { key: 'next', label: '下次执行', color: 'var(--brand)', value: next ? clock(next) : '—', note: next ? across(next) : '暂无待执行任务' },
  ]
})

const editing = ref<Schedule | null>(null)

const openForm = (mode: 'create' | 'edit', item?: Schedule) => {
  formMode.value = mode
  formError.value = ''
  editing.value = item || null
  form.value = item ? { ...item.config } : blankConfig()
  formOpen.value = true
}

const submitForm = async () => {
  formError.value = ''
  const config = { ...form.value }
  if (config.task_type === 'daily_report' && config.mode === 'daily' && !config.daily_time) {
    formError.value = '每日触发需要填写执行时刻'
    return
  }
  if (config.mode === 'interval' && !config.interval_minutes) {
    formError.value = '按间隔触发需要填写间隔分钟数'
    return
  }
  if (config.scope === 'baseline' && !config.station_code) {
    formError.value = '指定范围需要选择场站'
    return
  }
  if (!config.grant_days || config.grant_days < 1) {
    formError.value = '授权有效期至少 1 天'
    return
  }
  submitting.value = true
  try {
    const payload =
      formMode.value === 'edit' && editing.value
        ? await api.updateSchedule(editing.value, config)
        : await api.createSchedule(config)
    formOpen.value = false
    flash(`已提交审批（${payload.run_id}），批准后才会生效`)
    await load()
    // 提交后直接打开这条审查运行：审批就在本次操作里完成，不用再去找
    void openRun({ run_id: payload.run_id, status: payload.status, capability: 'schedule.propose' })
  } catch (error) {
    formError.value = String(error)
  } finally {
    submitting.value = false
  }
}

const act = async (item: Schedule, action: 'pause' | 'resume' | 'cancel') => {
  try {
    const updated = await api.scheduleAction(item, action)
    // pause/cancel 直接回任务快照；resume 走审批，回的是 run 回执
    if ('config' in updated) {
      const index = list.value.findIndex((row) => row.id === item.id)
      if (index >= 0) list.value[index] = updated
      if (detail.value?.id === item.id) detail.value = updated
    }
    const label = action === 'pause' ? '已暂停' : action === 'cancel' ? '已取消' : '已申请恢复'
    flash(`${item.id} ${label}${action === 'resume' ? `，等待批准（${'run_id' in updated ? updated.run_id : '—'}）` : ''}`)
  } catch (error) {
    flash(`操作失败：${String(error)}`)
  }
}

const openDetail = async (item: Schedule) => {
  detail.value = item
  history.value = []
  historyLoading.value = true
  try {
    const payload = await api.scheduleHistory(item.id)
    history.value = payload.list
  } catch {
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

/* ---------------- 最近的审查与预演 ---------------- */

const CAP_CN: Record<string, string> = {
  'schedule.propose': '配置审查',
  'rule.preview': '规则预演',
  'assistant.answer': '对话回答',
}
const RUN_STATUS_CN: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  awaiting_approval: '待批准',
  reconciling: '核查中',
  pending_reconciliation: '待核查',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
}
const runTone = (status: string) =>
  status === 'succeeded' ? 'ok'
    : status === 'awaiting_approval' ? 'warn'
      : status === 'failed' ? 'bad'
        : status === 'cancelled' ? 'idle' : 'brand'
/** 真后端是 UUID，取尾 8 位够用；mock 的 run-mock-4 这类短号直接去掉前缀，别显示成 n-mock-4 */
const shortRunID = (id: string) => (id.length > 12 ? id.slice(-8) : id.replace(/^run-/, ''))

const run = ref<RunSnapshot | null>(null)
const runCapability = ref('')
const runConnection = ref<RunConnectionState>('stopped')
const runError = ref('')
let runAbort: AbortController | null = null

const scrollRun = () => {
  void nextTick(() => document.querySelector('.run-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

/** 配置审查运行：审批、取消、产物都在 RunAnswer 里，这里只负责订阅与刷新 */
const openRun = async (item: ScheduleRunBrief) => {
  if (item.capability === 'rule.preview') {
    if (!previewOpen.value) togglePreview()
    await loadDevices()
    await openPreviewRun(item.run_id, true)
    return
  }
  runAbort?.abort()
  const abort = new AbortController()
  runAbort = abort
  runCapability.value = item.capability
  runError.value = ''
  try {
    const snapshot = await getRun(item.run_id, abort.signal)
    if (abort.signal.aborted) return
    run.value = snapshot
    scrollRun()
    if (!isTerminalStatus(snapshot.status)) {
      void consumeRun(
        item.run_id,
        snapshot,
        (value) => {
          if (abort.signal.aborted) return
          run.value = value
          if (isTerminalStatus(value.status)) void load()
        },
        abort.signal,
        (state) => { runConnection.value = state },
      ).catch((error) => { if (!abort.signal.aborted) runError.value = String(error) })
    }
  } catch (error) {
    runError.value = String(error)
  }
}

const onRunUpdated = (snapshot: RunSnapshot) => {
  run.value = snapshot
  // 批准/拒绝后任务表会变：批准生效、拒绝保留原配置
  if (isTerminalStatus(snapshot.status)) void load()
}
const stopRun = () => {
  runAbort?.abort()
  runConnection.value = 'stopped'
}
const resumeRun = () => {
  const current = run.value
  if (current) void openRun({ run_id: current.run_id, status: current.status, capability: runCapability.value })
}
const closeRun = () => {
  runAbort?.abort()
  run.value = null
}
const requestCancel = async () => {
  const current = run.value
  if (!current) return
  const ok = await askConfirm({
    title: '取消这次运行',
    body: '取消后本次审查不会生效，已产生的结果不会撤销。',
    confirmText: '请求取消',
    danger: true,
  })
  if (!ok) return
  try {
    run.value = await cancelRun(current.run_id)
    await load()
  } catch (error) {
    runError.value = String(error)
  }
}

/* ---------------- 方案预演 ---------------- */

/** datetime-local 的值按北京时间拼，不受本机时区影响 */
const bjInput = (ms: number) => {
  const at = new Date(ms + 8 * 3600_000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}T${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}`
}

const previewOpen = ref(sessionStorage.getItem('sched-preview-open') !== '0')
const previewForm = ref({
  device_id: 0,
  metric: 'temperature' as RulePreviewInput['metric'],
  start: '',
  end: '',
  operator: 'gt' as RulePreviewInput['operator'],
  threshold: 35,
  baseline_threshold: 40,
  expected_interval_seconds: 300,
})
const previewError = ref('')
const previewNotice = ref('')
const previewBusy = ref(false)
const previewResult = ref<RulePreviewResult | null>(null)
const previewRun = ref<RunSnapshot | null>(null)
const previewConnection = ref<RunConnectionState>('stopped')
const previewArtifactID = ref('')
let previewAbort: AbortController | null = null

/** 默认窗口取「已经结束的最近 24 小时」：无论几点打开，都有一整天的样本可比 */
const defaultWindow = () => {
  const end = Math.floor(Date.now() / 600_000) * 600_000
  previewForm.value.end = bjInput(end)
  previewForm.value.start = bjInput(end - 24 * 3600_000)
}
defaultWindow()

const devices = ref<Device[]>([])
const devicesLoading = ref(false)
let devicesLoaded = false

const loadDevices = async () => {
  if (devicesLoaded || devicesLoading.value) return
  devicesLoading.value = true
  try {
    const payload = await api.devices({ pageSize: 200 })
    devices.value = payload.list
    devicesLoaded = true
    if (!previewForm.value.device_id && payload.list.length) previewForm.value.device_id = payload.list[0].id
  } catch (error) {
    previewError.value = `设备列表未取到：${String(error)}`
  } finally {
    devicesLoading.value = false
  }
}

const togglePreview = () => {
  previewOpen.value = !previewOpen.value
  try {
    sessionStorage.setItem('sched-preview-open', previewOpen.value ? '1' : '0')
  } catch {
    /* 存储不可用时仅本次生效 */
  }
  if (previewOpen.value) void loadDevices()
}
onMounted(() => {
  if (previewOpen.value) void loadDevices()
})

const previewWindow = (hours: number) => {
  const end = Math.floor(Date.now() / 600_000) * 600_000
  previewForm.value.end = bjInput(end)
  previewForm.value.start = bjInput(end - hours * 3600_000)
  previewError.value = ''
}

const readPreviewResult = async (snapshot: RunSnapshot) => {
  previewNotice.value = ''
  previewRun.value = snapshot
  if (snapshot.status !== 'succeeded') {
    previewError.value = snapshot.error?.message || '预演未完成，可在「最近的审查与预演」里重新打开这条运行'
    return
  }
  const artifact = snapshot.artifacts.find((item) => String(item.name ?? '').endsWith('.json'))
  if (!artifact) {
    previewError.value = '预演没有产出结果文件'
    return
  }
  try {
    const data = await readArtifactJson<RulePreviewResult>(String(artifact.artifact_id), snapshot.run_id)
    previewResult.value = data
    previewArtifactID.value = String(artifact.artifact_id)
  } catch (error) {
    previewError.value = String(error)
  }
}

const openPreviewRun = async (runId: string, scroll = false) => {
  previewAbort?.abort()
  const abort = new AbortController()
  previewAbort = abort
  previewError.value = ''
  previewNotice.value = '读取运行状态…'
  previewResult.value = null
  previewArtifactID.value = ''
  try {
    const snapshot = await getRun(runId, abort.signal)
    if (abort.signal.aborted) return
    previewRun.value = snapshot
    previewNotice.value = snapshot.progress_message || ''
    if (isTerminalStatus(snapshot.status)) {
      await readPreviewResult(snapshot)
    } else {
      void consumeRun(
        runId,
        snapshot,
        (value) => {
          if (abort.signal.aborted) return
          previewRun.value = value
          previewNotice.value = value.progress_message || ''
          if (isTerminalStatus(value.status)) {
            void readPreviewResult(value)
            // 审查列表里这一条的状态要跟着落定，不能停在「排队中」
            void load()
          }
        },
        abort.signal,
        (state) => { previewConnection.value = state },
      ).catch((error) => { if (!abort.signal.aborted) previewError.value = String(error) })
    }
  } catch (error) {
    previewError.value = String(error)
    previewNotice.value = ''
  }
  if (scroll) void nextTick(() => document.querySelector('.preview-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

const submitPreview = async () => {
  previewError.value = ''
  const form = previewForm.value
  if (!form.device_id) {
    previewError.value = '请选择要预演的设备'
    return
  }
  const start = Date.parse(`${form.start}:00+08:00`)
  const end = Date.parse(`${form.end}:00+08:00`)
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    previewError.value = '请填写完整的开始与结束时间（北京时间，精确到分钟）'
    return
  }
  if (end <= start) {
    previewError.value = '结束时间必须晚于开始时间'
    return
  }
  if (end - start > 86_400_000) {
    previewError.value = '预演窗口最长 24 小时'
    return
  }
  if (end > Date.now()) {
    previewError.value = '只能预演已经发生的历史窗口'
    return
  }
  if (!Number.isInteger(form.threshold) || !Number.isInteger(form.baseline_threshold)) {
    previewError.value = '基准阈值与候选阈值必须是整数'
    return
  }
  if (!Number.isInteger(form.expected_interval_seconds) || form.expected_interval_seconds < 1 || form.expected_interval_seconds > 86_400) {
    previewError.value = '假设采样间隔必须在 1–86400 秒之间'
    return
  }
  previewBusy.value = true
  previewNotice.value = '正在提交只读预演…'
  try {
    const receipt = await api.previewRule({
      device_id: form.device_id,
      metric: form.metric,
      start: `${form.start}:00+08:00`,
      end: `${form.end}:00+08:00`,
      operator: form.operator,
      threshold: form.threshold,
      baseline_threshold: form.baseline_threshold,
      expected_interval_seconds: form.expected_interval_seconds,
    })
    await openPreviewRun(receipt.run_id)
    await load()
  } catch (error) {
    previewError.value = String(error)
    previewNotice.value = ''
  } finally {
    previewBusy.value = false
  }
}

const downloadPreview = async () => {
  const current = previewRun.value
  if (!current || !previewArtifactID.value) return
  try {
    await downloadArtifact(previewArtifactID.value, current.run_id)
  } catch (error) {
    previewError.value = String(error)
  }
}

const previewComplete = computed(() => previewResult.value?.coverage.complete === true)
const OP_CN: Record<string, string> = { gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于' }
/** 重新打开旧预演时，表单里是当前输入值，参数回显才说明这条结果当时按什么算的 */
const previewEcho = computed(() => {
  const result = previewResult.value
  if (!result) return ''
  const params = result.parameters
  const device = devices.value.find((item) => item.id === params.device_id)
  const unit = METRIC_UNIT[params.metric] || ''
  return [
    device ? `${device.device_code} · ${device.name}` : `设备 ${params.device_id}`,
    `${METRIC_CN[params.metric] || params.metric} ${OP_CN[params.operator] || params.operator} ${params.threshold}${unit}（基准 ${params.baseline_threshold}${unit}）`,
    `假设采样间隔 ${params.expected_interval_seconds} 秒`,
  ].join(' · ')
})

onUnmounted(() => {
  runAbort?.abort()
  previewAbort?.abort()
})
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" :disabled="loading" @click="load">
        <Icon name="refresh" :size="15" />刷新
      </button>
      <button class="btn btn-primary" @click="openForm('create')">
        <Icon name="plus" :size="15" />新建任务
      </button>
    </div>
  </div>

  <div v-if="failed" class="error-box">任务数据未取到：{{ failed }}</div>
  <div v-if="notice" class="notice">
    <Icon name="check" :size="14" />{{ notice }}
  </div>

  <section class="card strip">
    <div v-for="item in strip" :key="item.key" class="strip-cell">
      <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
      <span class="strip-value num">{{ item.value }}</span>
      <span class="strip-note">{{ item.note }}</span>
    </div>
  </section>

  <!-- 配置审查运行：审批就在本页完成，不用再跳到助手 -->
  <section v-if="run" class="card run-panel">
    <div class="card-head">
      <span class="card-title">当前运行</span>
      <span class="card-sub">{{ CAP_CN[runCapability] || runCapability }} · {{ run.run_id }}</span>
      <div class="card-tools">
        <button class="btn btn-sm" @click="closeRun">收起</button>
      </div>
    </div>
    <div class="run-body">
      <RunAnswer
        :run="run"
        :connection="runConnection"
        :error="runError"
        @updated="onRunUpdated"
        @stop="stopRun"
        @resume="resumeRun"
        @cancel="requestCancel"
      />
      <p class="run-foot">批准后任务才会生效；拒绝或取消都不影响已有任务与历史交付。</p>
    </div>
  </section>

  <div class="sc-grid">
    <div class="sc-main">
      <section class="card">
        <div class="card-head">
          <span class="card-title">我的任务</span>
          <span class="card-sub">{{ list.length }} 个 · 只列本人创建的任务</span>
        </div>

        <div class="task-list">
          <article v-for="item in list" :key="item.id" class="task" :class="`is-${item.status}`">
            <span class="task-icon" :class="`t-${statusTone(item.status)}`">
              <Icon :name="item.config.task_type === 'inspection' ? 'activity' : 'layers'" :size="17" />
            </span>

            <div class="task-main">
              <div class="task-title">
                <b>{{ typeName(item.config) }}</b>
                <StatusChip :tone="statusTone(item.status)" :label="STATUS_CN[item.status] || item.status" />
                <span class="task-ver num">v{{ item.config_version }}</span>
              </div>
              <p class="task-meta">
                {{ scopeName(item.config) }} · {{ triggerName(item.config) }} · 授权 {{ item.config.grant_days }} 天
              </p>
              <p class="task-note">
                <template v-if="item.status === 'active' && item.next_run_at">
                  下次执行 {{ stamp(item.next_run_at) }}（{{ across(item.next_run_at) }}）
                </template>
                <template v-else-if="item.reason">{{ item.reason }}</template>
                <template v-else>暂无排期</template>
                <span v-if="item.grant"> · 授权至 {{ stamp(item.grant.expires_at) }}</span>
              </p>
            </div>

            <div class="task-actions">
              <button class="btn btn-sm" @click="openDetail(item)">历史</button>
              <button
                v-if="item.status === 'active'"
                class="btn btn-sm"
                @click="act(item, 'pause')"
              >
                暂停
              </button>
              <button
                v-else-if="item.status === 'paused'"
                class="btn btn-sm"
                @click="act(item, 'resume')"
              >
                申请恢复
              </button>
              <button class="btn btn-sm" @click="openForm('edit', item)">修改</button>
              <button
                v-if="item.status !== 'cancelled'"
                class="btn btn-sm btn-quiet"
                @click="act(item, 'cancel')"
              >
                取消
              </button>
            </div>
          </article>

          <div v-if="!list.length && !loading" class="empty">还没有定期任务，从右上角新建一个</div>
        </div>
      </section>

      <!-- 方案预演：先试算再改规则 -->
      <section class="card preview-card">
        <div class="card-head">
          <span class="card-title">方案预演</span>
          <span class="card-sub">单设备 · 历史 24 小时内 · 只比较采样命中</span>
          <div class="card-tools">
            <button class="btn btn-sm" @click="togglePreview">
              <Icon name="chevronDown" :size="13" :style="{ transform: previewOpen ? 'none' : 'rotate(-90deg)' }" />
              {{ previewOpen ? '收起' : '展开' }}
            </button>
          </div>
        </div>

        <div v-show="previewOpen" class="preview-body">
          <p class="preview-note">
            先在这里试算候选阈值，再决定要不要动规则。预演只读历史快照、不改线上规则；
            命中次数是采样比较，不等于真实收益或漏报率。
          </p>

          <div class="pv-form">
            <label class="form-row span-2">
              <span>设备</span>
              <select v-model.number="previewForm.device_id" class="field" :disabled="devicesLoading">
                <option :value="0" disabled>{{ devicesLoading ? '读取设备…' : '请选择设备' }}</option>
                <option v-for="item in devices" :key="item.id" :value="item.id">
                  {{ item.device_code }} · {{ item.name }}
                </option>
              </select>
            </label>

            <label class="form-row">
              <span>指标</span>
              <select v-model="previewForm.metric" class="field">
                <option value="temperature">温度 ℃</option>
                <option value="power">功率 kW</option>
                <option value="voltage">电压 V</option>
                <option value="current">电流 A</option>
              </select>
            </label>

            <label class="form-row">
              <span>开始（北京时间，含）</span>
              <input v-model="previewForm.start" type="datetime-local" class="field" />
            </label>

            <label class="form-row">
              <span>结束（北京时间，不含）</span>
              <input v-model="previewForm.end" type="datetime-local" class="field" />
            </label>

            <label class="form-row">
              <span>假设采样间隔（秒）</span>
              <input v-model.number="previewForm.expected_interval_seconds" type="number" min="1" max="86400" step="1" class="field" />
            </label>

            <label class="form-row">
              <span>比较符</span>
              <select v-model="previewForm.operator" class="field">
                <option value="gt">大于 &gt;</option>
                <option value="gte">大于等于 ≥</option>
                <option value="lt">小于 &lt;</option>
                <option value="lte">小于等于 ≤</option>
              </select>
            </label>

            <label class="form-row">
              <span>基准阈值</span>
              <input v-model.number="previewForm.baseline_threshold" type="number" step="1" class="field" />
            </label>

            <label class="form-row">
              <span>候选阈值</span>
              <input v-model.number="previewForm.threshold" type="number" step="1" class="field" />
            </label>
          </div>

          <div class="preview-actions">
            <button class="btn btn-primary" :disabled="previewBusy" @click="submitPreview">
              {{ previewBusy ? '预演中…' : '创建只读预演' }}
            </button>
            <button class="btn btn-sm" @click="previewWindow(24)">近 24 小时</button>
            <button class="btn btn-sm" @click="previewWindow(1)">近 1 小时</button>
            <span class="preview-hint">采样间隔按实际上报周期填：填小了会把「没上报」算成缺测。</span>
          </div>

          <p v-if="previewError" class="preview-error">{{ previewError }}</p>
          <p v-else-if="previewNotice" class="preview-status">
            <i></i>{{ previewNotice }}<span v-if="previewConnection === 'reconnecting'"> · 连接中断，正在恢复</span>
          </p>

          <div v-if="previewResult" class="pv-result">
            <div class="pv-result-head">
              <b>预演结果</b>
              <StatusChip
                :dot="false"
                :tone="previewComplete ? 'ok' : 'warn'"
                :label="previewComplete ? '采样完整' : '采样不完整'"
              />
              <span class="pv-result-win num">
                {{ stamp(previewResult.coverage.start_inclusive) }} → {{ stamp(previewResult.coverage.end_exclusive) }}
              </span>
            </div>

            <p class="pv-summary">{{ previewEcho }}</p>

            <div class="pv-stats">
              <div class="pv-stat">
                <span>有效样本</span><b class="num">{{ n(previewResult.coverage.valid_samples) }}</b>
              </div>
              <div class="pv-stat">
                <span>预计缺测</span>
                <b class="num" :class="{ 'is-hot': previewResult.coverage.missing_samples > 0 }">
                  {{ n(previewResult.coverage.missing_samples) }}
                </b>
              </div>
              <div class="pv-stat">
                <span>基准命中</span><b class="num">{{ n(previewResult.metrics.baseline_hits) }}</b>
              </div>
              <div class="pv-stat">
                <span>候选命中</span><b class="num">{{ n(previewResult.metrics.candidate_hits) }}</b>
              </div>
            </div>

            <p class="pv-diff">
              候选新增命中 <b class="num">{{ n(previewResult.metrics.new_hits) }}</b>
              · 基准独有 <b class="num">{{ n(previewResult.metrics.baseline_only_hits) }}</b>
              · 区间断点 {{ n(previewResult.coverage.interior_gap_samples) }} 点
              · 快照 <span class="num">{{ previewResult.snapshot_sha256.slice(0, 12) }}</span>
            </p>

            <details class="pv-notes">
              <summary>假设与边界 · {{ previewResult.assumptions.length }} 条</summary>
              <ul>
                <li v-for="(item, index) in previewResult.assumptions" :key="index">{{ item }}</li>
              </ul>
            </details>

            <div class="pv-result-actions">
              <button class="btn btn-sm" @click="downloadPreview">下载结果 JSON</button>
              <span class="preview-hint">结论只对这段快照成立，未修改线上规则。</span>
            </div>
          </div>
        </div>
      </section>
    </div>

    <aside class="sc-side">
      <section class="card">
        <div class="card-head">
          <span class="card-title">授权与边界</span>
        </div>
        <div class="note-body">
          <p>任务只授予<b>只读</b>权限：读遥测、读告警、生成报表。不包含任何写设备、写规则的动作。</p>
          <p>授权到期后任务自动暂停，不会静默续期；恢复需要重新审批。</p>
          <p>交付位置固定为「平台站内」，暂不支持外部邮箱或其他系统。</p>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">最近的审查与预演</span>
          <span class="card-sub">最新 {{ runs.length }} 条 · 只列本人提交</span>
        </div>
        <div class="review-list">
          <button v-for="item in runs" :key="item.run_id" class="review" @click="openRun(item)">
            <span class="review-cap">{{ CAP_CN[item.capability] || item.capability }}</span>
            <span class="review-id num">{{ shortRunID(item.run_id) }}</span>
            <StatusChip :dot="false" :tone="runTone(item.status)" :label="RUN_STATUS_CN[item.status] || item.status" />
          </button>
          <div v-if="!runs.length" class="empty small">还没有配置审查或预演记录</div>
        </div>
      </section>
    </aside>
  </div>

  <!-- 任务详情 + 运行历史 -->
  <Drawer
    v-if="detail"
    :title="typeName(detail.config)"
    :sub="`${detail.id} · ${STATUS_CN[detail.status] || detail.status}`"
    @close="detail = null"
  >
    <dl class="kv">
      <dt>任务类型</dt>
      <dd>{{ typeName(detail.config) }}</dd>
      <dt>范围</dt>
      <dd>{{ scopeName(detail.config) }}</dd>
      <dt>触发</dt>
      <dd>{{ triggerName(detail.config) }}</dd>
      <dt>时区</dt>
      <dd class="num">{{ detail.config.timezone }}</dd>
      <dt>配置版本</dt>
      <dd class="num">v{{ detail.config_version }}</dd>
      <dt>授权有效期</dt>
      <dd class="num">{{ detail.config.grant_days }} 天</dd>
      <dt>授权到期</dt>
      <dd class="num">{{ detail.grant ? stamp(detail.grant.expires_at) : '—' }}</dd>
      <dt>交付位置</dt>
      <dd>{{ detail.config.destination_id === 'platform_client' ? '平台站内' : detail.config.destination_id }}</dd>
      <dt>最近原因</dt>
      <dd>{{ detail.reason || '—' }}</dd>
    </dl>

    <h3 class="section-title">运行历史</h3>
    <div v-if="historyLoading" class="loading">读取历史…</div>
    <table v-else-if="history.length" class="table history-table">
      <colgroup>
        <col style="width: 40%" />
        <col style="width: 22%" />
        <col style="width: 38%" />
      </colgroup>
      <thead>
        <tr><th>计划时间</th><th>结果</th><th>说明</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in history" :key="index">
          <td class="num">{{ stamp(row.scheduled_for) }}</td>
          <td>
            <StatusChip
              :tone="row.status === 'delivered' ? 'ok' : 'idle'"
              :label="row.status === 'delivered' ? '已投递' : '已跳过'"
            />
          </td>
          <td class="muted">{{ row.reason || (row.run_id ? `运行 ${row.run_id.slice(-6)}` : '—') }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty small">暂无历史</div>

    <template #footer>
      <button v-if="detail.status === 'active'" class="btn" @click="act(detail, 'pause')">暂停任务</button>
      <button v-else-if="detail.status === 'paused'" class="btn" @click="act(detail, 'resume')">申请恢复</button>
      <button class="btn btn-primary" @click="openForm('edit', detail); detail = null">修改配置</button>
    </template>
  </Drawer>

  <!-- 新建 / 修改 -->
  <Drawer
    v-if="formOpen"
    :title="formMode === 'edit' ? '修改任务配置' : '新建定期任务'"
    sub="提交后生成待批运行，批准前不会执行"
    @close="formOpen = false"
  >
    <div class="form">
      <label class="form-row">
        <span>任务类型</span>
        <select v-model="form.task_type" class="field">
          <option value="daily_report">定期日报</option>
          <option value="inspection">定期巡检</option>
        </select>
      </label>

      <label class="form-row">
        <span>范围</span>
        <select v-model="form.scope" class="field">
          <option value="all">全部场站</option>
          <option value="business">业务场站</option>
          <option value="baseline">指定场站</option>
        </select>
      </label>

      <label v-if="form.scope === 'baseline'" class="form-row">
        <span>场站</span>
        <select v-model="form.station_code" class="field">
          <option :value="null">请选择</option>
          <option v-for="item in stations" :key="item.station_code" :value="item.station_code">
            {{ item.station_code }} · {{ item.station_name }}
          </option>
        </select>
      </label>

      <label class="form-row">
        <span>触发方式</span>
        <select v-model="form.mode" class="field">
          <option value="daily">每日定时</option>
          <option value="interval">按间隔</option>
        </select>
      </label>

      <label v-if="form.mode === 'daily'" class="form-row">
        <span>执行时刻</span>
        <input v-model="form.daily_time" type="time" class="field" />
      </label>

      <label v-else class="form-row">
        <span>间隔（分钟）</span>
        <input v-model.number="form.interval_minutes" type="number" min="5" step="5" class="field" />
      </label>

      <label class="form-row">
        <span>授权有效期（天）</span>
        <input v-model.number="form.grant_days" type="number" min="1" max="90" class="field" />
      </label>

      <div class="form-row is-static">
        <span>交付位置</span>
        <b>平台站内</b>
      </div>

      <div v-if="formError" class="form-error">{{ formError }}</div>

      <div class="form-note">
        任务只申请只读授权；到期自动暂停。修改配置会生成新版本并重新走审批。
      </div>
    </div>

    <template #footer>
      <button class="btn" @click="formOpen = false">取消</button>
      <button class="btn btn-primary" :disabled="submitting" @click="submitForm">
        {{ submitting ? '提交中…' : '提交审批' }}
      </button>
    </template>
  </Drawer>
</template>

<style scoped>
.strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.strip-cell {
  padding: 15px 20px 14px;
  border-left: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.strip-cell:first-child {
  border-left: 0;
}

.strip-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--ink-3);
}

.strip-label i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.strip-value {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
  line-height: 1.25;
}

.strip-note {
  font-size: 11px;
  color: var(--ink-4);
}

.notice {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  border-radius: var(--r-md);
  background: var(--ok-soft);
  border: 1px solid #bbf7d0;
  color: var(--ok-deep);
  font-size: 12px;
}

.sc-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 322px;
  gap: 16px;
  align-items: start;
}

.sc-main {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.sc-side {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.task-list {
  display: flex;
  flex-direction: column;
  padding: 2px 12px 12px;
}

.task {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 13px 10px;
  border-radius: var(--r-md);
  border-bottom: 1px solid var(--line-soft);
  transition: background 0.15s ease;
}

.task:last-child {
  border-bottom: 0;
}

.task:hover {
  background: var(--card-hover);
}

.task.is-cancelled {
  opacity: 0.72;
}

.task-icon {
  width: 34px;
  height: 34px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  flex: 0 0 34px;
  background: var(--brand-soft);
  color: var(--brand);
}

.task-icon.t-ok {
  background: var(--ok-soft);
  color: var(--ok-deep);
}

.task-icon.t-warn {
  background: var(--warn-soft);
  color: var(--warn-deep);
}

.task-icon.t-idle {
  background: var(--card-soft);
  color: var(--ink-3);
}

.task-main {
  flex: 1;
  min-width: 0;
}

.task-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.task-title b {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--ink);
}

.task-ver {
  font-size: 10.5px;
  color: var(--ink-4);
  border: 1px solid var(--line);
  border-radius: var(--r-pill);
  padding: 0 6px;
}

.task-meta {
  margin-top: 3px;
  font-size: 12px;
  color: var(--ink-2);
}

.task-note {
  margin-top: 2px;
  font-size: 11px;
  color: var(--ink-3);
}

.task-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
  max-width: 260px;
}

.btn-quiet {
  color: var(--bad);
}

.btn-quiet:hover {
  border-color: #fecaca;
  background: var(--bad-soft);
}

/* 当前运行：RunAnswer 自带边框，这里只做容器与脚注 */
.run-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 20px 16px;
}

.run-foot {
  font-size: 11px;
  color: var(--ink-4);
}

/* 最近的审查与预演：一行一条，点击打开运行或预演结果 */
.review-list {
  display: flex;
  flex-direction: column;
  padding: 2px 12px 14px;
}

.review {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 0;
  border-radius: var(--r-md);
  border-bottom: 1px solid var(--line-soft);
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease;
}

.review:last-child {
  border-bottom: 0;
}

.review:hover {
  background: var(--card-hover);
}

.review:focus-visible {
  outline: 2px solid var(--brand-soft);
  outline-offset: -2px;
}

.review-cap {
  font-size: 12px;
  color: var(--ink);
  font-weight: 550;
}

.review-id {
  font-size: 11px;
  color: var(--ink-3);
}

.note-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 20px 18px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.note-body b {
  color: var(--ink-2);
}

/* 方案预演 */
.preview-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 0 20px 18px;
}

.preview-note {
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink-3);
}

.pv-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 14px;
}

.pv-form .span-2 {
  grid-column: span 2;
}

.preview-actions,
.pv-result-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.preview-hint {
  font-size: 11px;
  line-height: 1.5;
  color: var(--ink-4);
}

.preview-error {
  padding: 8px 11px;
  border-radius: var(--r-sm);
  background: var(--bad-soft);
  color: var(--bad-deep);
  font-size: 11.5px;
  line-height: 1.6;
}

.preview-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.preview-status i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--brand);
  animation: pv-pulse 1.1s ease-in-out infinite;
}

@keyframes pv-pulse {
  0%,
  100% {
    opacity: 0.35;
  }

  50% {
    opacity: 1;
  }
}

.pv-result {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 13px 14px 14px;
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--card-2);
}

.pv-result-head {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}

.pv-result-head b {
  font-size: 13px;
  color: var(--ink);
}

.pv-result-win {
  margin-left: auto;
  font-size: 11px;
  color: var(--ink-4);
}

.pv-summary {
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink-2);
}

.pv-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--card);
}

.pv-stat {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 9px 11px;
  border-left: 1px solid var(--line-soft);
}

.pv-stat:first-child {
  border-left: 0;
}

.pv-stat span {
  font-size: 10.5px;
  color: var(--ink-4);
}

.pv-stat b {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
}

.pv-stat b.is-hot {
  color: var(--warn-deep);
}

.pv-diff {
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.pv-diff b {
  color: var(--ink);
}

.pv-notes {
  font-size: 11.5px;
  color: var(--ink-3);
}

.pv-notes summary {
  cursor: pointer;
  color: var(--ink-2);
}

.pv-notes ul {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 7px 0 0;
  padding-left: 18px;
  line-height: 1.65;
}

.section-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--ink-2);
  margin: 16px 0 8px;
}

.history-table {
  table-layout: fixed;
}

.history-table td {
  font-size: 12px;
}

.muted {
  color: var(--ink-3);
}

.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.form-row > span {
  font-size: 11.5px;
  color: var(--ink-3);
}

.form-row .field {
  min-width: 0;
  width: 100%;
}

.form-row.is-static b {
  font-size: 13px;
  color: var(--ink);
}

.form-error {
  padding: 8px 11px;
  border-radius: var(--r-sm);
  background: var(--bad-soft);
  color: var(--bad-deep);
  font-size: 11.5px;
}

.form-note {
  font-size: 11px;
  line-height: 1.6;
  color: var(--ink-4);
}

.empty.small {
  font-size: 11.5px;
  color: var(--ink-4);
  padding: 10px 0;
}

@media (max-width: 1240px) {
  .sc-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .sc-side {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* 三列里的时间输入放不下就退两列：datetime-local 太窄会截断小时分钟，设备也收回一格免得落单 */
@media (max-width: 1180px) {
  .pv-form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .pv-form .span-2 {
    grid-column: auto;
  }
}

@media (max-width: 900px) {
  .task {
    flex-wrap: wrap;
  }

  .task-actions {
    max-width: none;
    width: 100%;
    justify-content: flex-start;
    padding-left: 46px;
  }
}

@media (max-width: 760px) {
  .strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .strip-cell:nth-child(3) {
    border-left: 0;
    border-top: 1px solid var(--line-soft);
  }

  .strip-cell:nth-child(4) {
    border-top: 1px solid var(--line-soft);
  }

  .sc-side {
    grid-template-columns: minmax(0, 1fr);
  }

  .pv-form {
    grid-template-columns: minmax(0, 1fr);
  }

  .pv-form .span-2 {
    grid-column: auto;
  }

  .pv-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .pv-stat:nth-child(3) {
    border-left: 0;
    border-top: 1px solid var(--line-soft);
  }

  .pv-stat:nth-child(4) {
    border-top: 1px solid var(--line-soft);
  }
}
</style>
