<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import Icon from './Icon.vue'
import StatusChip from './StatusChip.vue'
import RiskBandsChart from './RiskBandsChart.vue'
import DriftReading from './DriftReading.vue'
import QualityDialog from './QualityDialog.vue'
import QualityTrend from './QualityTrend.vue'
import Pager from './Pager.vue'
import { api } from '../lib/api'
import { shortVersion } from '../lib/version'
import type {
  DriftEvent,
  PredictionModelVersion,
  PredictionQualitySeries,
  PredictionQualitySummary,
  RiskBand,
  QualityAnalysis,
  QualityOperations,
  TrainEligibility,
} from '../lib/api'
import { isAdmin } from '../lib/auth'
import { n, pct, stamp } from '../lib/format'
import { askConfirm, toast } from '../lib/toast'
import { useLatestRequest } from '../lib/useLatestRequest'

const DAY = 86_400_000

type RangeKey = 7 | 30 | 90
const ranges: { days: RangeKey; label: string }[] = [
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
  { days: 90, label: '近 90 天' },
]
const days = ref<RangeKey>(30)

const summaryRequest = useLatestRequest<PredictionQualitySummary>()
const seriesRequest = useLatestRequest<PredictionQualitySeries>()
const riskBandsRequest = useLatestRequest<{ items: RiskBand[]; samples: number; faults: number; baseline_rate: number }>()
const modelsRequest = useLatestRequest<{ items: PredictionModelVersion[]; runtime_version: string }>()
const eligibilityRequest = useLatestRequest<TrainEligibility>()
const training = ref(false)
const eligibility = computed(() => eligibilityRequest.data.value)
const driftRequest = useLatestRequest<DriftEvent[]>()
const operationsRequest = useLatestRequest<QualityOperations>()
const operations = computed(() => operationsRequest.data.value)

const summary = computed(() => summaryRequest.data.value)
const points = computed(() => seriesRequest.data.value?.points ?? [])
const seriesVersion = computed(() => seriesRequest.data.value?.model_version ?? '')
const riskBands = computed(() => riskBandsRequest.data.value)
const models = computed(() => modelsRequest.data.value?.items ?? [])
/** 推理服务实际加载的版本；与台账现役不一致时必须提示，否则会静默跑错模型 */
const runtimeVersion = computed(() => modelsRequest.data.value?.runtime_version ?? '')
const ledgerActive = computed(() => models.value.find((item) => item.status === 'active')?.model_version ?? '')
/** 台账有现役但运行版本读不到：健康检查不可读或未返回版本，不能假装一致 */
const versionUnknown = computed(() => Boolean(ledgerActive.value && !runtimeVersion.value))
const versionMismatch = computed(
  () => Boolean(runtimeVersion.value && ledgerActive.value && runtimeVersion.value !== ledgerActive.value),
)
const drift = computed(() => driftRequest.data.value ?? [])

/* 漂移事件分页与归档：列表默认只返回未归档行，归档只打标记、不删除原始观察记录 */
const DRIFT_PAGE_SIZE = 10
const driftPage = ref(1)
const driftTotal = ref(0)
const pickedDrift = ref<Set<number>>(new Set())
const archivingDrift = ref(false)

const loading = computed(
  () => summaryRequest.pending.value || seriesRequest.pending.value || riskBandsRequest.pending.value,
)

/** 失败按区块列出来，全部拼成一行：错误最多一行，重试一次全拉 */
const failed = computed(() =>
  [
    { label: '质量汇总', error: summaryRequest.error.value },
    { label: '按天趋势', error: seriesRequest.error.value },
    { label: '高风险组命中率', error: riskBandsRequest.error.value },
    { label: '模型版本', error: modelsRequest.error.value },
    { label: '漂移事件', error: driftRequest.error.value },
    { label: '重训条件', error: eligibilityRequest.error.value },
    { label: '近期误报', error: operationsRequest.error.value },
    { label: '漂移解读', error: driftAnalysisRequest.error.value },
    { label: '试用解读', error: trialAnalysisRequest.error.value },
  ]
    .filter((item) => item.error)
    .map((item) => `${item.label}：${item.error}`)
    .join('；'),
)

/** 窗口按「现在往前推 N 天」算，展示的是发起请求时那一刻的区间，而不是渲染时刻 */
const range = shallowRef<{ from: string; to: string } | null>(null)
const windowLabel = computed(() => (range.value ? `${stamp(range.value.from)} 至 ${stamp(range.value.to)}` : '—'))

const loadCore = async () => {
  const to = new Date()
  const from = new Date(to.getTime() - days.value * DAY)
  const params = { from: from.toISOString(), to: to.toISOString() }
  range.value = params
  await summaryRequest.run((signal) => api.predictionQualitySummary(params, signal))
  // 批次趋势跟随汇总实际采用的版本，避免 KPI 与曲线各自挑一个版本。
  if (range.value !== params) return
  void seriesRequest.run((signal) =>
    api.predictionQualitySeries(days.value, signal, summary.value?.model_version ?? undefined),
  )
  void riskBandsRequest.run((signal) =>
    api.predictionQualityRiskBands(days.value, signal, summary.value?.model_version ?? undefined),
  )
}

/** 版本与漂移不随窗口变化，进页面拉一次即可；重拉时保留旧行，避免整表先闪成空 */
const loadModels = () =>
  modelsRequest.run(async (signal) => await api.predictionQualityModels(signal), { keepData: true })
const loadDrift = () =>
  driftRequest.run(
    async (signal) => {
      const result = await api.predictionQualityDrift(driftPage.value, DRIFT_PAGE_SIZE, signal)
      driftTotal.value = result.total
      return result.items
    },
    { keepData: true },
  )

const driftPages = computed(() => Math.max(1, Math.ceil(driftTotal.value / DRIFT_PAGE_SIZE)))
const allDriftPicked = computed(
  () => drift.value.length > 0 && drift.value.every((item) => pickedDrift.value.has(item.id)),
)

watch(driftPage, () => {
  pickedDrift.value = new Set()
  void loadDrift()
})

const togglePickDrift = (id: number) => {
  const next = new Set(pickedDrift.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  pickedDrift.value = next
}

const togglePickAllDrift = () => {
  pickedDrift.value = allDriftPicked.value ? new Set() : new Set(drift.value.map((item) => item.id))
}

/** 归档只打标记：原始观察记录保留，列表默认不再返回；失败项不中断其余 */
const archivePickedDrift = async () => {
  const ids = [...pickedDrift.value]
  if (!ids.length || archivingDrift.value) return
  const confirmed = await askConfirm({
    title: `归档 ${ids.length} 条漂移事件`,
    body: '归档只打标记，原始观察记录保留；归档后默认列表不再显示这些事件。',
    confirmText: '归档',
  })
  if (!confirmed) return
  archivingDrift.value = true
  try {
    const result = await api.archiveDrifts(ids)
    if (result.failed.length) {
      toast('warn', `已归档 ${result.archived} 条，${result.failed.length} 条未能归档`)
    } else {
      toast('ok', `已归档 ${result.archived} 条漂移事件`)
    }
    pickedDrift.value = new Set()
    /* 整页都被归档时往回退一页，避免停在一张空表上 */
    if (result.archived >= drift.value.length && driftPage.value > 1) driftPage.value -= 1
    else await loadDrift()
  } catch (error) {
    toast('bad', `归档失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    archivingDrift.value = false
  }
}
const loadEligibility = () =>
  eligibilityRequest.run(async (signal) => await api.predictionQualityTrainEligibility(signal), { keepData: true })

/** 只取最近 1 天：值班员看的是「今天每台设备大概报几次假警」，不是长窗口均值 */
const loadOperations = () =>
  operationsRequest.run(async (signal) => await api.predictionQualityOperations(1, signal), { keepData: true })

const driftAnalysisRequest = useLatestRequest<QualityAnalysis[]>()
const trialAnalysisRequest = useLatestRequest<QualityAnalysis[]>()
const analyzing = ref<'drift' | 'trial' | ''>('')
const driftAnalysis = computed(() => driftAnalysisRequest.data.value?.[0] ?? null)
const trialAnalysis = computed(() => trialAnalysisRequest.data.value?.[0] ?? null)

const loadAnalysis = () => {
  driftAnalysisRequest.run(async (signal) => (await api.predictionQualityAnalysisLatest('drift', signal)).items, { keepData: true })
  trialAnalysisRequest.run(async (signal) => (await api.predictionQualityAnalysisLatest('trial', signal)).items, { keepData: true })
}

/** AI 解读只写 analysis 表，不碰模型与台账；失败时页面保留原始指标 */
const runAnalysis = async (kind: 'drift' | 'trial') => {
  if (analyzing.value) return
  analyzing.value = kind
  try {
    const result = await api.predictionQualityAnalysis(kind)
    toast('ok', `解读完成：${result.conclusion}`)
    await loadAnalysis()
  } catch (error) {
    toast('bad', `解读失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    analyzing.value = ''
  }
}

/** 数据不够时按钮置灰，并直接展示后端给的原因，不在前端拼文案 */
const startTrain = async () => {
  if (training.value || !eligibility.value?.ready) return
  training.value = true
  try {
    const result = await api.predictionQualityTrain()
    if (result.eligible) {
      toast('ok', `训练完成，候选 ${shortVersion(result.model_version)} 已登记`)
      void loadModels()
    } else {
      const reason = result.rejection_reason || '未达准入'
      toast('bad', `训练完成但未过准入：${reason}`)
    }
  } catch (error) {
    toast('bad', `重训失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    training.value = false
    void loadEligibility()
  }
}

const reload = () => {
  loadCore()
  void loadModels()
  void loadDrift()
  void loadEligibility()
  void loadAnalysis()
  void loadOperations()
}

onMounted(reload)
watch(days, loadCore)

/* ---------------- 指标与首屏读数 ---------------- */

/** 比例一律转百分比；null 是「算不出来」，显示「—」*/
const ratioOf = (value: number | null | undefined) => (value == null ? '—' : pct(value * 100, 1))

/** 窗口口径漏报率 = 1 − 召回率，只在口径说明里作参考 */
const missRate = computed(() => {
  const recall = summary.value?.recall
  return recall == null ? null : 1 - recall
})

const batch = computed(() => summary.value?.batch ?? null)
const hasBatch = computed(() => Boolean(batch.value && batch.value.total > 0))
const batchHitRate = computed(() => (hasBatch.value ? ratioOf(batch.value!.rate) : '—'))
const section = ref<'drift' | 'models' | 'windows' | 'analysis'>('drift')
const sections = [
  { key: 'drift', label: '漂移事件' }, { key: 'models', label: '模型版本' },
  { key: 'windows', label: '窗口评估' }, { key: 'analysis', label: 'AI 解读与重训' },
] as const
const analysisOpen = ref(false)
const selectedVersion = ref('')
const selectedModel = computed(() => models.value.find(item => item.model_version === selectedVersion.value))
const glossaryOpen = ref(false)
const outcomes = computed(() => {
  const s = summary.value
  if (!s) return []
  return [
    { label: '实际发生故障', correct: s.tp, error: s.fn, hit: '提前报出', miss: '漏报' },
    { label: '实际无故障', correct: s.tn, error: s.fp, hit: '正确排除', miss: '误报' },
  ]
})
const metricNames: Record<string, string> = {
  precision: '精确率', recall: '召回率', f1: 'F1', accuracy: '准确率',
  auc: 'AUC', roc_auc: 'ROC AUC', pr_auc: 'PR AUC', ece: '校准误差',
  threshold: '判定阈值', samples: '样本量', positives: '正样本量',
}
const modelMetricKeys = computed(() => Array.from(new Set([
  ...Object.keys(selectedModel.value?.metrics ?? {}), ...Object.keys(selectedModel.value?.baseline_metrics ?? {}),
])))
const formatMetric = (key: string, value: unknown) => {
  if (value == null) return '—'
  if (typeof value !== 'number') return typeof value === 'object' ? JSON.stringify(value) : String(value)
  if (['precision', 'recall', 'accuracy'].includes(key)) return ratioOf(value)
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 5 })
}

/** 运行态徽标：一致 / 不一致 / 读不到，三种都写清，不静默当成一致 */
const runtimeState = computed(() => {
  if (versionMismatch.value) return { tone: 'bad', label: '版本不一致' }
  if (versionUnknown.value) return { tone: 'warn', label: '运行版本待核对' }
  if (runtimeVersion.value && ledgerActive.value) return { tone: 'ok', label: '运行一致' }
  return { tone: 'idle', label: '待核对' }
})

const activeModel = computed(() => models.value.find((item) => item.status === 'active') ?? null)
/** 越线触发处置的才算醒目事件；只登记观察的保持中性色 */
const isTriggered = (item: DriftEvent) => item.action === 'triggered' || item.level === 'triggered'
/* ---------------- 模型版本 ---------------- */

const MODEL_STATUS: Record<string, { label: string; tone: string }> = {
  active: { label: '现役', tone: 'ok' },
  candidate: { label: '候选', tone: 'brand' },
  trial: { label: '试用中', tone: 'warn' },
  retired: { label: '已退役', tone: 'idle' },
  rejected: { label: '已拒绝', tone: 'bad' },
}
const statusOf = (status: string) => MODEL_STATUS[status] ?? { label: status, tone: 'idle' }

const DATA_SOURCE_CN: Record<string, string> = {
  synthetic: '合成数据',
  production: '生产数据',
  backfill: '历史回填',
  replay: '历史回放',
}
const sourceText = (item: PredictionModelVersion) =>
  item.data_source ? DATA_SOURCE_CN[item.data_source] || item.data_source : '—'

const promoting = ref('')

/* ---------------- 试用与回退 ---------------- */

/** 试用：切过去并开始 24 小时观察。失败时后端不会改台账，这里只提示原因。 */
const trialModel = async (item: PredictionModelVersion) => {
  const target = item.model_version
  const ok = await askConfirm({
    title: `试用 ${target}`,
    body: '切换后立即生效并开始 24 小时观察，不满意可一键回退到上一版。',
    confirmText: '开始试用',
  })
  if (!ok) return
  promoting.value = target
  try {
    const result = await api.trialModel(target)
    toast('ok', `已切到试用：${shortVersion(result.model_version)}，观察 24 小时`)
    await loadModels()
  } catch (error) {
    toast('bad', `试用失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    promoting.value = ''
  }
}

const confirmTrial = async (item: PredictionModelVersion) => {
  const target = item.model_version
  const ok = await askConfirm({
    title: `确认 ${shortVersion(target)} 转正`,
    body: '确认后该版本成为现役，原现役改为已退役。建议先看试用期的命中率再决定。',
    confirmText: '确认转正',
  })
  if (!ok) return
  promoting.value = target
  try {
    const result = await api.confirmModel(target)
    toast('ok', `${shortVersion(result.model_version)} 已转正`)
    await loadModels()
  } catch (error) {
    toast('bad', `转正失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    promoting.value = ''
  }
}

const rollbackTrial = async (item: PredictionModelVersion) => {
  const target = item.model_version
  const ok = await askConfirm({
    title: `回退 ${shortVersion(target)}`,
    body: `将恢复到 ${item.previous_version ? shortVersion(item.previous_version) : '上一版本'}，该版本回到候选状态。回退不重启服务。`,
    confirmText: '回退',
  })
  if (!ok) return
  promoting.value = target
  try {
    const result = await api.rollbackModel(target)
    toast('ok', `已回退到 ${shortVersion(result.rolled_back_to)}`)
    await loadModels()
  } catch (error) {
    toast('bad', `回退失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    promoting.value = ''
  }
}

/* ---------------- 漂移事件 ---------------- */

/** 信号 / 动作 / 层级来自后端枚举，认识的翻成中文，不认识的原样显示（不猜语义） */
const SIGNAL_CN: Record<string, string> = {
  probability: '预测均值',
  prob_mean: '平均概率',
  high_ratio: '高风险占比',
  precision: '命中率',
  recall: '召回率',
  f1: 'F1',
  accuracy: '准确率',
  ece: '校准误差',
  positive_rate: '真实故障率',
}
const ACTION_CN: Record<string, string> = {
  triggered: '已触发',
  watch: '观察',
  retrain: '安排重训',
  promote: '切换版本',
  rollback: '回滚版本',
  hold: '维持现役',
  review: '人工复核',
  none: '无需处理',
}
/** L1 = 分布漂移，L2 = 校准漂移，L3 = 精度退化；说明放 title，不占列宽 */
const LEVEL_HINT: Record<string, string> = {
  L1: 'L1 分布漂移：24 小时预测均值或高风险占比越出经验区间',
  L2: 'L2 校准漂移：连续三天校准误差高于基线',
  L3: 'L3 精度退化：连续三天命中率低于基线 10 个百分点',
}
const signalText = (value: string) => SIGNAL_CN[value] || value
const actionText = (value: string | null) => (value ? ACTION_CN[value] || value : '—')
const levelHint = (value: string) => LEVEL_HINT[value] || value
</script>

<template>
  <div class="quality">
    <header class="toolbar">
      <h1>模型质量</h1>
      <div class="controls">
        <div class="seg" aria-label="统计周期">
          <button v-for="r in ranges" :key="r.days" :aria-pressed="days === r.days" :class="{ 'is-on': days === r.days }" @click="days = r.days">{{ r.label }}</button>
        </div>
        <button class="btn" :disabled="loading" @click="reload"><Icon name="refresh" :size="15" />刷新</button>
      </div>
    </header>
    <div v-if="failed" class="error-box" role="alert">{{ failed }} <button class="btn btn-sm" @click="reload">重试</button></div>

    <section class="card overview">
      <div class="score">
        <h2>故障提前发现率</h2>
        <strong class="rate num">{{ batchHitRate }}</strong>
        <p v-if="hasBatch && batch">提前发现 <b>{{ n(batch.hit) }}</b> 批 <span class="divider">/</span> 漏报 <b>{{ n(batch.total - batch.hit) }}</b> 批</p>
        <p v-else>{{ summaryRequest.pending.value ? '正在统计…' : batch ? '本期无故障批次' : '暂无批次数据' }}</p>
      </div>
      <div class="trend">
        <div class="section-heading"><h2>按日趋势</h2><span>故障批次 · 近 {{ days }} 天</span></div>
        <QualityTrend :points="points" :pending="seriesRequest.pending.value" :height="142" />
      </div>
      <dl class="metrics">
        <div><dt>窗口准确率</dt><dd>{{ ratioOf(summary?.accuracy) }}</dd></div>
        <div><dt>中位提前量 · 近 1 天</dt><dd>{{ operations?.lead_samples ? n(operations.lead_minutes_p50, 1) + ' 分' : '—' }}</dd></div>
        <div><dt>每台误报 · 近 1 天</dt><dd>{{ operations?.devices ? n(operations.false_alarms_per_device_day, 1) + ' 次' : '—' }}</dd></div>
        <div><dt>已评估窗口</dt><dd>{{ n(summary?.samples) }}</dd></div>
      </dl>
    </section>

    <div class="runtime">
      <div><span>台账现役</span> <button v-if="activeModel" class="version-link" @click="selectedVersion = activeModel.model_version">{{ shortVersion(activeModel.model_version) }} ↗</button><b v-else>—</b></div>
      <StatusChip :tone="runtimeState.tone" :label="runtimeState.label" />
      <span v-if="versionMismatch">实际加载 {{ runtimeVersion }}</span>
      <span v-if="summary?.model_version && summary.model_version !== ledgerActive">统计版本 {{ summary.model_version }}</span>
    </div>
    <nav class="section-nav" aria-label="模型质量分区">
      <button v-for="s in sections" :key="s.key" :aria-pressed="section === s.key" @click="section = s.key">{{ s.label }} <b v-if="s.key === 'drift' && driftRequest.data.value">{{ n(driftTotal) }}</b><b v-if="s.key === 'models' && modelsRequest.data.value">{{ models.length }}</b></button>
    </nav>

    <section v-if="section === 'drift'" class="card panel">
      <div class="section-heading">
        <h2>漂移记录</h2>
        <div class="controls" v-if="drift.length">
          <label class="pick"><input type="checkbox" :checked="allDriftPicked" @change="togglePickAllDrift" /> 全选本页</label>
          <button v-if="pickedDrift.size" class="btn btn-sm" :disabled="archivingDrift" @click="archivePickedDrift">{{ archivingDrift ? '归档中…' : '归档选中 ' + pickedDrift.size + ' 条' }}</button>
        </div>
      </div>
      <article v-for="event in drift" :key="event.id" class="event">
        <input type="checkbox" :aria-label="'选择事件 ' + event.id" :checked="pickedDrift.has(event.id)" @change="togglePickDrift(event.id)" />
        <div class="event-id">
          <div><b>{{ signalText(event.signal) }}</b> <span :title="levelHint(event.level)">{{ event.level }}</span></div>
          <time>{{ stamp(event.detected_at) }}</time>
        </div>
        <DriftReading :event="event" />
        <StatusChip :tone="isTriggered(event) ? 'bad' : event.action === 'watch' ? 'warn' : 'idle'" :label="isTriggered(event) && event.action !== 'triggered' ? '已触发 · ' + actionText(event.action) : actionText(event.action)" />
      </article>
      <p v-if="!drift.length" class="empty">{{ driftRequest.pending.value ? '正在读取…' : driftRequest.error.value ? '漂移记录暂不可用' : '没有未归档的漂移事件' }}</p>
      <Pager v-if="drift.length" :page="driftPage" :total-pages="driftPages" :total="driftTotal" :page-size="DRIFT_PAGE_SIZE" @update:page="driftPage = $event" />
    </section>

    <section v-if="section === 'models'" class="card panel">
      <div class="section-heading"><h2>版本台账</h2></div>
      <div v-for="model in models" :key="model.model_version" class="model-row">
        <button class="model-name" @click="selectedVersion = model.model_version"><strong>{{ model.model_version }}</strong><span>{{ sourceText(model) }} · {{ stamp(model.trained_at) }}</span></button>
        <StatusChip :tone="statusOf(model.status).tone" :label="statusOf(model.status).label" />
        <button class="btn btn-sm" @click="selectedVersion = model.model_version">查看详情</button>
        <button v-if="isAdmin && model.status === 'candidate'" class="btn btn-sm" :disabled="!!promoting" @click="trialModel(model)">开始试用</button>
        <template v-if="isAdmin && model.status === 'trial'">
          <button class="btn btn-sm" :disabled="!!promoting" @click="confirmTrial(model)">确认转正</button>
          <button class="btn btn-sm" :disabled="!!promoting" @click="rollbackTrial(model)">回退</button>
        </template>
      </div>
      <p v-if="!models.length" class="empty">{{ modelsRequest.pending.value ? '正在读取…' : modelsRequest.error.value ? '模型台账暂不可用' : '暂无登记版本' }}</p>
    </section>

    <div v-if="section === 'windows'" class="split">
      <section class="card panel">
        <div class="section-heading"><h2>预测窗口结果</h2><span>窗口结束后 1 小时内的真值</span></div>
        <div v-for="row in outcomes" :key="row.label" class="outcome">
          <div class="section-heading"><b>{{ row.label }}</b><span>{{ n(row.correct + row.error) }} 个窗口</span></div>
          <div class="outcome-bar" aria-hidden="true" :class="{ populated: row.correct + row.error > 0 }">
            <i :style="{ width: (row.correct + row.error ? 100 * row.correct / (row.correct + row.error) : 0) + '%' }"></i>
          </div>
          <div class="tally"><span>{{ row.hit }} <b>{{ n(row.correct) }}</b></span><span>{{ row.miss }} <b>{{ n(row.error) }}</b></span></div>
        </div>
        <p v-if="!outcomes.length" class="empty">{{ summaryRequest.pending.value ? '正在统计…' : '暂无窗口数据' }}</p>
      </section>
      <section class="card panel"><div class="section-heading"><h2>高风险组命中率</h2></div>
        <RiskBandsChart v-if="riskBands" :bands="riskBands.items" :samples="riskBands.samples" :faults="riskBands.faults" :baseline-rate="riskBands.baseline_rate" compact />
        <p v-else class="empty">{{ riskBandsRequest.pending.value ? '正在统计…' : '暂无风险分组数据' }}</p>
      </section>
    </div>

    <div v-if="section === 'analysis'" class="split">
      <section class="card panel">
        <div class="section-heading"><h2>AI 解读</h2></div>
        <p>{{ driftAnalysis?.conclusion || trialAnalysis?.conclusion || '暂无解读记录' }}</p>
        <div class="controls"><button class="btn" :disabled="!!analyzing" @click="runAnalysis('drift')">{{ analyzing === 'drift' ? '分析中…' : '分析漂移' }}</button>
          <button class="btn" :disabled="!!analyzing" @click="runAnalysis('trial')">{{ analyzing === 'trial' ? '分析中…' : '对比试用' }}</button>
          <button v-if="driftAnalysis || trialAnalysis" class="version-link" @click="analysisOpen = true">完整解读 ↗</button>
        </div>
      </section>
      <section class="card panel">
        <div class="section-heading"><h2>训练候选模型</h2></div>
        <p v-if="eligibility">{{ eligibility.days }} 天 · {{ n(eligibility.samples) }} 条样本 · {{ n(eligibility.positives) }} 个正样本</p>
        <p v-if="eligibility && !eligibility.ready">{{ eligibility.blockers.join('；') }}</p>
        <button class="btn" :disabled="training || !eligibility?.ready" @click="startTrain">{{ training ? '训练中…' : '开始重训' }}</button>
      </section>
    </div>
    <!-- 口径说明：原文一条不删，只是不占正文 -->
    <section class="card glossary">
      <button class="glossary-summary" :aria-expanded="glossaryOpen" @click="glossaryOpen = !glossaryOpen">
        <span class="card-title">统计口径与说明</span>

        <span class="glossary-toggle">{{ glossaryOpen ? '收起' : '展开' }}</span>
      </button>
      <div v-if="glossaryOpen" class="glossary-body">
        <p>统计窗口 {{ windowLabel }} · 统计版本 {{ summary?.model_version || '—' }} · 趋势版本 {{ seriesVersion || '—' }}</p>
        <p><b>样本量</b>：窗口结束后满 1 小时、按 1 小时真值规则标注的预测。</p>
        <p><b>故障提前叫出（批次命中率）</b>：1 小时内提前叫出的故障批次 ÷ 总故障批次；同一台设备同一次故障算一批，分母含提前量窗口。中位提前量取各批次提前分钟数的中位数。</p>
        <p><b>窗口漏报率 / 批次漏报率</b>：批次漏报率 = 1 − 批次命中率；窗口漏报率 = 1 − 召回率，两者口径不同，不可混用。</p>
        <p><b>窗口准确率</b>：(TP + TN) ÷ 全部窗口；窗口命中 {{ ratioOf(summary?.precision) }}、窗口漏报 {{ ratioOf(missRate) }}。</p>
        <p><b>每台设备日均误报</b>：误报窗口数 ÷ 设备数 ÷ 天数（固定取最近 1 天，与顶部统计窗口选择无关）；当前 {{ operations?.false_alarms ?? '—' }} 次误报窗口 ÷ {{ operations?.devices ?? '—' }} 台设备 ÷ {{ operations?.days ?? '—' }} 天。</p>
        <p><b>每台设备日均误报事件</b>：合并相邻高分窗口（间隔≤10分钟记一起）：{{ operations?.false_alarm_events ?? '—' }} 起误报事件，来自 {{ operations?.false_alarm_event_windows ?? '—' }} 个高分窗口（含命中真实故障的窗口，非纯误报窗口）；另有 {{ operations?.false_alarm_events_with_fault ?? '—' }} 起事件含真实故障、不计入误报（共 {{ operations?.false_alarm_event_total ?? '—' }} 起）。</p>
        <p><b>平均预警提前量</b>：中位数，90 分位 {{ operations?.lead_minutes_p90 ?? '—' }} 分钟，1 小时内提前发现 {{ operations?.captured ?? '—' }}/{{ operations?.faults ?? '—' }} 次故障。</p>
        <p><b>窗口结果</b>：按阈值判定，窗口口径（真值只看结束后 1 小时，分母含提前量窗口）。每条按实际有无故障分别计算占比。命中率与漏报率此消彼长：降低阈值可减少漏报，代价是误报增加。</p>
        <p><b>高风险组命中率</b>：累积增益曲线，横轴是抽查设备比例，纵轴是抓到故障比例；虚线为随机抽查基准。共 {{ riskBands?.samples ?? 0 }} 条样本、真实故障 {{ riskBands?.faults ?? 0 }} 次。</p>
        <p><b>按日趋势</b>：按故障批次统计，仅看 1 小时内是否被提前叫出；缺失日期不画点，也不跨日期连线。</p>
        <p><b>离线精确率 / 离线漏报率</b>：来自模型训练报告（多为合成数据），仅证明合成场景下的训练效果；真实运行效果以页面顶部按 1 小时真值统计的线上故障批次命中率与误报事件为准，二者口径不同、不可混用，也不代表实盘已验证。</p>
        <p><b>模型版本操作</b>：候选只能先试用，试用会实际切换并核对推理服务运行版本，试用期满后确认转正；退役与被拒版本仅作留档，不支持回退。</p>
        <p><b>版本一致性</b>：台账现役与推理服务实际加载版本不一致时，以推理服务实际运行版本为准，必要时检查 /health 是否反映最新加载；运行版本读不到时不作「已一致」判断。</p>
        <p><b>漂移事件</b>：观测值与基线采用同一口径；观察级事件仅登记，处置需人工确认。L1 分布漂移、L2 校准漂移、L3 精度退化，详见表内层级提示。</p>
      </div>
    </section>


    <QualityDialog v-if="selectedModel" title="模型版本详情" @close="selectedVersion = ''">
      <div class="detail-id"><strong>{{ selectedModel.model_version }}</strong><StatusChip :tone="statusOf(selectedModel.status).tone" :label="statusOf(selectedModel.status).label" /></div>
      <dl class="facts">
        <dt>实际运行</dt><dd>{{ !runtimeVersion ? '无法核对' : runtimeVersion === selectedModel.model_version ? '当前已加载' : '当前加载 ' + runtimeVersion }}</dd>
        <dt>训练来源</dt><dd>{{ sourceText(selectedModel) }}</dd>
        <dt>训练样本</dt><dd>{{ n(selectedModel.train_samples) }} · 正样本 {{ n(selectedModel.positive_samples) }}</dd>
        <dt>判定阈值</dt><dd>{{ ratioOf(selectedModel.threshold) }}</dd>
        <dt>训练时间</dt><dd>{{ stamp(selectedModel.trained_at) }}</dd>
        <dt v-if="selectedModel.promoted_at">上线时间</dt><dd v-if="selectedModel.promoted_at">{{ stamp(selectedModel.promoted_at) }} {{ selectedModel.promoted_by }}</dd>
        <dt v-if="selectedModel.trial_started_at">试用开始</dt><dd v-if="selectedModel.trial_started_at">{{ stamp(selectedModel.trial_started_at) }} · 观察期 24 小时</dd>
        <dt v-if="selectedModel.previous_version">前序版本</dt><dd v-if="selectedModel.previous_version">{{ selectedModel.previous_version }}</dd>
        <dt v-if="selectedModel.rejected_reason">拒绝原因</dt><dd v-if="selectedModel.rejected_reason">{{ selectedModel.rejected_reason }}</dd>
        <dt v-if="selectedModel.artifact_path">模型文件</dt><dd v-if="selectedModel.artifact_path">{{ selectedModel.artifact_path }}</dd>
      </dl>
      <h3>离线评估</h3>
      <table v-if="modelMetricKeys.length" class="table metric-table"><thead><tr><th>指标</th><th>模型</th><th>训练基线</th></tr></thead>
        <tbody><tr v-for="key in modelMetricKeys" :key="key"><td>{{ metricNames[key] || key }}</td><td>{{ formatMetric(key, selectedModel.metrics?.[key]) }}</td><td>{{ formatMetric(key, selectedModel.baseline_metrics?.[key]) }}</td></tr></tbody>
      </table>
      <p v-else>未登记离线评估指标</p>
    </QualityDialog>
    <QualityDialog v-if="analysisOpen" title="完整解读" @close="analysisOpen = false">
      <template v-for="entry in [{ title: '漂移归因', data: driftAnalysis }, { title: '试用对比', data: trialAnalysis }]" :key="entry.title">
        <section v-if="entry.data"><h3>{{ entry.title }}</h3><p>{{ entry.data.conclusion }}</p>
          <template v-if="entry.data.reasons.length"><b>依据</b><ul><li v-for="(reason, i) in entry.data.reasons" :key="i">{{ reason }}</li></ul></template>
          <template v-if="entry.data.suggestions.length"><b>建议</b><ol><li v-for="(item, i) in entry.data.suggestions" :key="i">{{ item }}</li></ol></template>
          <small>{{ stamp(entry.data.created_at) }} · {{ entry.data.prompt_tokens + entry.data.completion_tokens }} tokens</small>
        </section>
      </template>
    </QualityDialog>
  </div>
</template>

<style scoped>
.quality { display:grid; gap:16px; color:var(--ink); min-width:0 }
.toolbar,.section-heading,.controls,.runtime,.detail-id,.tally { display:flex; gap:12px; align-items:center; flex-wrap:wrap }
.toolbar,.section-heading,.tally { justify-content:space-between }
.toolbar h1 { margin:0; font-size:clamp(24px,2.7vw,36px); line-height:1.2; letter-spacing:-.035em }
.quality h2 { font-size:16px; line-height:1.4; margin:0; font-weight:650 }
.overview { display:grid; grid-template-columns:minmax(220px,.72fr) minmax(0,1.65fr); overflow:hidden }
.score,.trend,.panel { padding:clamp(16px,2vw,26px); min-width:0 }
.score { border-inline-end:1px solid var(--line-soft); align-content:center }
.rate { display:block; margin:10px 0; font-size:clamp(38px,5vw,70px); line-height:1; letter-spacing:-.055em; color:var(--brand-deep) }
.score p { margin:0; font-size:14px }
.divider { margin:0 8px; color:var(--ink-3) }
.trend .section-heading { margin-bottom:6px }
.metrics { grid-column:1/-1; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-top:1px solid var(--line); margin:0; padding:16px 0 }
.metrics div { padding:0 clamp(12px,2vw,26px) }
.metrics div+div { border-inline-start:1px solid var(--line-soft) }
.metrics dt,.section-heading>span,.model-name span,time,.runtime>div>span,.pick,.facts dt { color:var(--ink-2); font-size:13px }
.metrics dd { margin:6px 0 0; font-size:clamp(19px,2.1vw,30px); font-variant-numeric:tabular-nums; font-weight:620; line-height:1.15 }
.runtime { font-size:14px; overflow-wrap:anywhere }
.version-link,.model-name,.section-nav button,.glossary-summary { cursor:pointer; background:transparent; border:0; color:inherit; font:inherit }
.version-link { color:var(--brand-deep); text-decoration:underline; text-underline-offset:3px }
.section-nav { display:flex; gap:clamp(8px,2vw,28px); border-bottom:1px solid var(--line); overflow-x:auto }
.section-nav button { flex:none; padding:10px 3px; color:var(--ink-2); border-bottom:3px solid transparent; font-weight:600 }
.section-nav button[aria-pressed=true] { color:var(--brand-deep); border-bottom-color:var(--brand) }
.section-nav b { font-size:12px; font-variant-numeric:tabular-nums }
.event { display:grid; grid-template-columns:18px minmax(150px,.9fr) minmax(240px,1.8fr) minmax(65px,auto); align-items:center; gap:clamp(10px,2vw,24px); padding:18px 0; border-top:1px solid var(--line-soft) }
.panel>.section-heading { margin-bottom:14px }
.event-id span { font-size:12px; color:var(--ink-2) }
time,.model-name span { display:block; margin-top:4px }
.event input,.pick input { accent-color:var(--brand) }
.model-row { display:flex; align-items:center; gap:14px; border-top:1px solid var(--line-soft); padding:14px 0; flex-wrap:wrap }
.model-name { flex:1; min-width:140px; padding:0; text-align:left; overflow-wrap:anywhere }
.model-name:hover strong { text-decoration:underline }
.split { display:grid; grid-template-columns:1fr 1fr; gap:16px }
.outcome { margin:20px 0 }
.outcome-bar { height:12px; margin:9px 0; background:var(--track-bg); border-radius:6px; overflow:hidden }
.outcome-bar.populated { background:var(--warn) }
.outcome-bar i { display:block; height:100%; background:var(--brand) }
.tally { font-size:13px }
.glossary { padding:0; overflow:hidden }
.glossary-summary { width:100%; padding:14px 20px; display:flex; align-items:center; justify-content:space-between }
.glossary-toggle { color:var(--brand-deep) }
.glossary-body { padding:0 20px 12px; font-size:14px; line-height:1.65 }
.detail-id { overflow-wrap:anywhere }
.facts { display:grid; grid-template-columns:auto minmax(0,1fr); gap:10px 22px; padding:18px 0; border-block:1px solid var(--line-soft) }
.facts dd { margin:0; overflow-wrap:anywhere }
.metric-table { width:100%; table-layout:fixed; overflow-wrap:anywhere }
.metric-table td { white-space:normal }
@media(max-width:850px) {
  .event { grid-template-columns:18px minmax(0,1fr) auto }
  .event .reading { grid-column:2/-1; grid-row:2 }
  .split { grid-template-columns:1fr }
}
@media(max-width:630px) {
  .quality { gap:12px }
  .overview { grid-template-columns:1fr }
  .score { border:0; border-bottom:1px solid var(--line-soft) }
  .metrics { grid-template-columns:1fr 1fr; gap:16px 0 }
  .metrics div:nth-child(3) { border:0 }
  .section-nav { gap:8px }
  .section-nav button { font-size:13px }
}
</style>
