<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import StatusChip from './StatusChip.vue'
import RiskBandsChart from './RiskBandsChart.vue'
import ConfusionMatrix from './ConfusionMatrix.vue'
import QualityTrend from './QualityTrend.vue'
import Pager from './Pager.vue'
import { api } from '../lib/api'
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

/**
 * 模型质量：只看「已经到期、有真实结果」的预测，与线上故障预测分开统计。
 * 五个区块各自独立请求：任何一块失败都不该让其余区块变成空白，
 * 所以错误只汇总成一行文案 + 重试，不逐块弹 toast。
 *
 * 信息层级（2026-11-08 重排）：首屏只放「现在质量如何」「要不要动手」，
 * 图表与完整台账下移；所有解释性文字收进页尾可展开的「统计口径」，不占正文。
 */

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
    { label: '校准曲线', error: riskBandsRequest.error.value },
    { label: '模型版本', error: modelsRequest.error.value },
    { label: '漂移事件', error: driftRequest.error.value },
  ]
    .filter((item) => item.error)
    .map((item) => `${item.label}：${item.error}`)
    .join('；'),
)

/** 窗口按「现在往前推 N 天」算，展示的是发起请求时那一刻的区间，而不是渲染时刻 */
const range = ref<{ from: string; to: string } | null>(null)
const windowLabel = computed(() => (range.value ? `${stamp(range.value.from)} 至 ${stamp(range.value.to)}` : '—'))

const loadCore = async () => {
  const to = new Date()
  const from = new Date(to.getTime() - days.value * DAY)
  const params = { from: from.toISOString(), to: to.toISOString() }
  range.value = params
  await summaryRequest.run((signal) => api.predictionQualitySummary(params, signal))
  // 批次趋势跟随汇总实际采用的版本，避免 KPI 与曲线各自挑一个版本。
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
      toast('ok', `训练完成，候选 ${result.model_version ?? ''} 已登记`)
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
const batchLine = computed(() => {
  const value = batch.value
  if (!hasBatch.value || !value) return '区间内没有故障批次'
  const lead = value.p50_lead_minutes
  return `${value.total} 批中叫出 ${value.hit} 批${lead == null ? '' : ` · 中位提前 ${lead} 分钟`}`
})

/**
 * 批次格：每个方块代表一个故障批次，绿色=提前叫出、红色=漏报。
 * 一屏内用「格子的颜色比例」代替「90%、9/10」这类百分比说明，
 * 运维不用读数字就知道漏了几次。超过 40 批只画前 40 格，文字仍给全量。
 */
const BATCH_CELLS_MAX = 40
const batchCells = computed(() => {
  const value = batch.value
  if (!hasBatch.value || !value) return []
  const total = Math.min(value.total, BATCH_CELLS_MAX)
  const hit = Math.min(value.hit, total)
  return Array.from({ length: total }, (_, index) => index < hit)
})

/** 三条可量化的进度条：给刻度上限才能画出「离满还有多远」，没上限的样本量不进条 */
const miniBars = computed(() => {
  const value = summary.value
  const ops = operations.value
  const accuracy = value?.accuracy
  const lead = ops?.lead_minutes_p50
  const falseAlarms = ops?.false_alarms_per_device_day ?? null
  return [
    {
      label: '窗口准确率',
      value: value ? ratioOf(accuracy) : '—',
      hint: '窗口口径：(TP+TN) ÷ 全部窗口',
      pct: accuracy == null ? 0 : Math.max(0, Math.min(100, accuracy * 100)),
      color: 'var(--brand)',
    },
    {
      label: '中位提前量',
      value: ops ? `${lead} 分` : '—',
      hint: '满格 = 1 小时提前量上限，越接近满越好',
      pct: lead == null ? 0 : Math.max(0, Math.min(100, (lead / 60) * 100)),
      color: 'var(--ok)',
    },
    {
      label: '每台日均误报',
      value: falseAlarms == null ? '—' : `${falseAlarms.toFixed(1)} 次`,
      hint: '满格 = 每台每天 20 次，越短越好',
      pct: falseAlarms == null ? 0 : Math.max(0, Math.min(100, (falseAlarms / 20) * 100)),
      color: 'var(--warn)',
    },
  ]
})

/** 格子条下面只留一行计数，避免把批次口径再写成长句 */
const batchCaption = computed(() => {
  const value = batch.value
  if (!hasBatch.value || !value) return '区间内无故障批次'
  return `${value.total} 批 · 命中 ${value.hit} · 漏 ${value.total - value.hit}`
})

/** 卡片里只放结论，完整解读进大弹窗：小格子塞不下，也不该塞 */
const analysisOpen = ref(false)
const closeAnalysis = () => {
  analysisOpen.value = false
}
const onEsc = (event: KeyboardEvent) => {
  if (event.key === 'Escape') closeAnalysis()
}
watch(analysisOpen, (open) => {
  if (open) window.addEventListener('keydown', onEsc)
  else window.removeEventListener('keydown', onEsc)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onEsc))

/** 运行态徽标：一致 / 不一致 / 读不到，三种都写清，不静默当成一致 */
const runtimeState = computed(() => {
  if (versionMismatch.value) return { tone: 'bad', label: '版本不一致' }
  if (versionUnknown.value) return { tone: 'warn', label: '运行版本待核对' }
  if (runtimeVersion.value && ledgerActive.value) return { tone: 'ok', label: '运行一致' }
  return { tone: 'idle', label: '待核对' }
})

const activeModel = computed(() => models.value.find((item) => item.status === 'active') ?? null)
const trialModelRow = computed(() => models.value.find((item) => item.status === 'trial') ?? null)

/** 越线触发处置的才算醒目事件；只登记观察的保持中性色 */
const isTriggered = (item: DriftEvent) => item.action === 'triggered' || item.level === 'triggered'
const triggeredDrift = computed(() => drift.value.filter(isTriggered))
/** 有触发级优先只看触发级，否则退回最近三条；首屏不堆整张表 */
const recentDrift = computed(() => (triggeredDrift.value.length ? triggeredDrift.value : drift.value).slice(0, 3))

const driftSection = ref<HTMLElement | null>(null)
const modelsSection = ref<HTMLElement | null>(null)
const glossaryOpen = ref(false)
const scrollTo = (target: 'drift' | 'models') => {
  const node = target === 'drift' ? driftSection.value : modelsSection.value
  node?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

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

const metricOf = (item: PredictionModelVersion, key: string) => ratioOf(item.metrics?.[key])
const missOf = (item: PredictionModelVersion) => {
  const recall = item.metrics?.recall
  return recall == null ? '—' : pct((1 - recall) * 100, 1)
}

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
    toast('ok', `已切到试用：${result.model_version}，观察 24 小时`)
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
    title: `确认 ${target} 转正`,
    body: '确认后该版本成为现役，原现役改为已退役。建议先看试用期的命中率再决定。',
    confirmText: '确认转正',
  })
  if (!ok) return
  promoting.value = target
  try {
    const result = await api.confirmModel(target)
    toast('ok', `${result.model_version} 已转正`)
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
    title: `回退 ${target}`,
    body: `将恢复到 ${item.previous_version ?? '上一版本'}，该版本回到候选状态。回退不重启服务。`,
    confirmText: '回退',
  })
  if (!ok) return
  promoting.value = target
  try {
    const result = await api.rollbackModel(target)
    toast('ok', `已回退到 ${result.rolled_back_to}`)
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
  <div class="qq">
    <div class="qq-bar">
      <div class="seg">
        <button v-for="item in ranges" :key="item.days" :class="{ 'is-on': days === item.days }" @click="days = item.days">
          {{ item.label }}
        </button>
      </div>
      <span class="qq-window">
        统计窗口 {{ windowLabel }}
        <template v-if="summary?.model_version"> · 统计版本 <b class="num">{{ summary.model_version }}</b></template>
      </span>
      <button class="btn btn-sm qq-refresh" :disabled="loading" @click="reload">
        <Icon name="refresh" :size="13" />刷新
      </button>
    </div>

    <div v-if="failed" class="error-box qq-error" role="alert">
      <span>模型质量数据未取到：{{ failed }}</span>
      <button class="btn btn-sm" @click="reload">重试</button>
    </div>

    <!-- 首屏 A：主数字 + 批次格（一格格的颜色比例就是命中率）+ 三条进度条 -->
    <section class="card hero">
      <div class="hero-main">
        <div class="hero-eyebrow">质量速览 <span>按故障批次 · 近 {{ days }} 天</span></div>
        <div class="hero-body">
          <div class="hero-lead">
            <span class="hero-value num">{{ batchHitRate }}</span>
            <div class="hero-lead-side">
              <div v-if="batchCells.length" class="batch-cells" :title="batchLine">
                <i v-for="(hit, index) in batchCells" :key="index" :class="hit ? 'is-hit' : 'is-miss'"></i>
              </div>
              <span class="batch-caption num" :title="batchLine">{{ batchCaption }}</span>
            </div>
          </div>

          <div class="hero-bars">
            <div v-for="bar in miniBars" :key="bar.label" class="mini" :title="bar.hint">
              <span class="mini-label">{{ bar.label }}</span>
              <span class="mini-track"><i :style="{ width: `${bar.pct}%`, background: bar.color }"></i></span>
              <b class="mini-value num">{{ bar.value }}</b>
            </div>
          </div>
        </div>
      </div>
      <div class="hero-side">
        <span class="side-label">运行态核对</span>
        <div class="side-version">
          <b class="num">{{ ledgerActive || '—' }}</b>
          <StatusChip :tone="runtimeState.tone" :label="runtimeState.label" />
        </div>
        <p v-if="versionMismatch" class="side-warn">
          推理服务实际加载 <b class="num">{{ runtimeVersion }}</b>，与台账现役不一致，请以运行版本为准。
        </p>
        <p v-else-if="versionUnknown" class="side-warn is-soft">
          推理服务运行版本暂无法核对（健康检查不可读或未返回版本），不要据此认为版本已一致。
        </p>
        <p v-else class="side-ok">台账与推理服务版本一致</p>
        <div class="side-meta">
          <span>统计窗口</span><b class="num">{{ windowLabel }}</b>
          <span>样本量</span><b class="num">{{ summary ? n(summary.samples) : '—' }}</b>
        </div>
      </div>
    </section>

    <!-- 首屏 B：高信息量模块。左「读完就要动手」，右「需要盯的异常」 -->
    <div class="focus-grid">
      <section class="card focus-card">
        <div class="card-head">
          <span class="card-title">AI 解读</span>
          <span class="card-sub">漂移归因与试用对比 · 只给建议</span>
          <div class="card-tools">
            <button class="btn btn-sm" :disabled="analyzing !== ''" @click="runAnalysis('drift')">
              {{ analyzing === 'drift' ? '分析中…' : '分析漂移' }}
            </button>
            <button class="btn btn-sm btn-quiet" :disabled="analyzing !== ''" @click="runAnalysis('trial')">
              {{ analyzing === 'trial' ? '分析中…' : '对比试用' }}
            </button>
          </div>
        </div>
        <!-- 小卡片只放结论：完整解读点开弹窗看，正文不再挤在这里 -->
        <div
          class="insight"
          :class="{ 'is-clickable': !!driftAnalysis }"
          @click="driftAnalysis && (analysisOpen = true)"
        >
          <template v-if="driftAnalysis">
            <p class="insight-conclusion clamp-2" :title="driftAnalysis.conclusion">{{ driftAnalysis.conclusion }}</p>
            <p class="insight-peek">
              <span class="num">{{ driftAnalysis.reasons.length }}</span> 条依据 ·
              <span class="num">{{ driftAnalysis.suggestions.length }}</span> 条建议
              <template v-if="trialAnalysis"> · 含试用对比</template>
              <span class="insight-open">阅读完整解读 →</span>
            </p>
          </template>
          <p v-else class="insight-empty">还没有解读记录，点「分析漂移」让模型读一遍分时误差</p>
        </div>
        <div class="retrain-row">
          <div class="retrain-text">
            <b>用新数据重训</b>
            <span v-if="eligibility" class="retrain-note" :class="eligibility.ready ? 'is-ok' : 'is-blocked'">
              {{ eligibility.days }} 天 / {{ n(eligibility.samples) }} 条 / {{ n(eligibility.positives) }} 个正样本 ·
              {{ eligibility.ready ? '已达准入门槛' : eligibility.blockers.join('；') }}
            </span>
            <span v-else class="retrain-note">正在读取数据量…</span>
          </div>
          <button
            class="btn btn-sm"
            :disabled="!eligibility?.ready || training"
            :title="eligibility?.ready ? '按最近 120 天窗口模式数据重训一份候选' : (eligibility?.blockers.join('；') || '正在读取数据量')"
            @click="startTrain"
          >
            {{ training ? '训练中…' : '开始重训' }}
          </button>
        </div>
      </section>

      <section class="card focus-card">
        <div class="card-head">
          <span class="card-title">版本与漂移</span>
          <span class="card-sub">现役 / 试用 · 最近触发级事件</span>
        </div>
        <div class="version-panel">
          <div v-if="activeModel" class="version-row">
            <StatusChip :tone="statusOf(activeModel.status).tone" :label="statusOf(activeModel.status).label" />
            <b class="version-name num">{{ activeModel.model_version }}</b>
            <span class="version-meta num">
              阈值 {{ activeModel.threshold == null ? '—' : pct(activeModel.threshold * 100, 0) }} ·
              离线精确率 {{ metricOf(activeModel, 'precision') }} · 漏报率 {{ missOf(activeModel) }}
            </span>
          </div>
          <div v-if="trialModelRow" class="version-row">
            <StatusChip :tone="statusOf(trialModelRow.status).tone" :label="statusOf(trialModelRow.status).label" />
            <b class="version-name num">{{ trialModelRow.model_version }}</b>
            <span class="version-meta num">观察 24 小时后再决定转正或回退</span>
          </div>
          <p v-if="versionMismatch" class="version-warn">
            运行版本 <b class="num">{{ runtimeVersion }}</b> 与台账现役不一致，必要时检查 /health 是否反映最新加载。
          </p>
          <p v-else-if="versionUnknown" class="version-warn is-soft">运行版本暂无法核对，请以推理服务实际加载为准。</p>
          <button class="link-btn" @click="scrollTo('models')">查看模型台账（{{ models.length }} 个版本）</button>
        </div>
        <div class="drift-panel">
          <div class="drift-panel-head">
            <b>最近漂移</b>
            <span>{{ triggeredDrift.length ? `${triggeredDrift.length} 条触发级` : '无触发级事件' }}</span>
          </div>
          <div v-if="recentDrift.length" class="drift-list">
            <div v-for="item in recentDrift" :key="item.id" class="drift-item">
              <div class="drift-main">
                <span class="drift-title">
                  <b class="num" :title="levelHint(item.level)">{{ item.level }}</b>
                  {{ signalText(item.signal) }}
                </span>
                <span class="drift-value num">
                  观测 {{ n(item.observed, 3) }} · 基线 {{ n(item.baseline, 3) }} · 阈值 {{ n(item.threshold, 3) }}
                </span>
                <span class="drift-time num">{{ stamp(item.detected_at) }}</span>
              </div>
              <StatusChip :tone="isTriggered(item) ? 'bad' : 'idle'" :label="actionText(item.action)" />
            </div>
          </div>
          <p v-else class="drift-empty">{{ driftRequest.pending.value ? '正在读取漂移事件…' : '窗口内无漂移事件' }}</p>
        </div>
        <button class="link-btn focus-foot" @click="scrollTo('drift')">查看全部漂移事件（{{ drift.length }} 条）</button>
      </section>
    </div>

    <!-- 下滑 C：明细图表。压缩并排，功能不减、不折叠 -->
    <div class="section-title">
      <h2>明细指标</h2>
      <span>图表按预测窗口口径统计，与上方故障批次口径不同，仅用于核对</span>
    </div>
    <div class="detail-grid">
      <section class="card">
        <div class="card-head">
          <span class="card-title">混淆矩阵</span>
          <span class="card-sub">窗口口径 · 真值只看结束后 1 小时</span>
        </div>
        <div class="cm-wrap">
          <ConfusionMatrix
            compact
            :tp="summary?.tp ?? 0"
            :fp="summary?.fp ?? 0"
            :fn="summary?.fn ?? 0"
            :tn="summary?.tn ?? 0"
          />
          <p class="cm-note">颜色深浅按对数刻度映射样本量；降低阈值可减少漏报，代价是误报增加。</p>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">高风险组命中率</span>
          <span class="card-sub">
            共 {{ riskBands?.samples ?? 0 }} 条 · 真实故障 {{ riskBands?.faults ?? 0 }} 次 ·
            平均预测概率 {{ ratioOf(summary?.prob_mean) }}
          </span>
        </div>
        <p v-if="riskBandsRequest.pending.value && !riskBands" class="bands-loading">正在统计…</p>
        <RiskBandsChart
          v-else
          compact
          :bands="riskBands?.items ?? []"
          :samples="riskBands?.samples ?? 0"
          :faults="riskBands?.faults ?? 0"
          :baseline-rate="riskBands?.baseline_rate ?? 0"
        />
      </section>
    </div>

    <section class="card trend-card">
      <div class="card-head">
        <span class="card-title">按日趋势</span>
        <span class="card-sub">
          近 {{ days }} 天 · 按故障批次（仅 1 小时内是否被提前叫出）
          <template v-if="seriesVersion"> · 版本 <b class="num">{{ seriesVersion }}</b></template>
        </span>
      </div>
      <QualityTrend :points="points" :pending="seriesRequest.pending.value" :height="156" />
    </section>

    <!-- 下滑 D：完整台账 -->
    <div class="section-title">
      <h2>台账与事件</h2>
      <span>完整版本台账与漂移记录，操作仍需逐行确认</span>
    </div>

    <section ref="modelsSection" class="card">
      <div class="card-head">
        <span class="card-title">模型版本</span>
        <span class="card-sub">{{ models.length }} 个版本 · 候选可试用，试用期满可转正</span>
      </div>
      <div class="table-wrap">
        <table class="table md-table">
          <colgroup>
            <col style="width: 15%" />
            <col style="width: 11%" />
            <col style="width: 10%" />
            <col style="width: 12%" />
            <col style="width: 8%" />
            <col style="width: 8%" />
            <col style="width: 16%" />
            <col style="width: 12%" />
            <col style="width: 8%" />
          </colgroup>
          <thead>
            <tr>
              <th>版本</th>
              <th>状态</th>
              <th>数据来源</th>
              <th>训练样本</th>
              <th>离线精确率</th>
              <th>离线漏报率</th>
              <th>文件位置</th>
              <th>上线时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in models" :key="item.model_version">
              <td>
                <div class="cell-stack">
                  <span class="cell-name num">{{ item.model_version }}</span>
                  <span class="cell-note">阈值 {{ item.threshold == null ? '—' : pct(item.threshold * 100, 0) }}</span>
                </div>
              </td>
              <td>
                <div class="cell-stack">
                  <StatusChip :tone="statusOf(item.status).tone" :label="statusOf(item.status).label" />
                  <span v-if="item.status === 'rejected' && item.rejected_reason" class="cell-note" :title="item.rejected_reason">
                    {{ item.rejected_reason }}
                  </span>
                </div>
              </td>
              <td>{{ sourceText(item) }}</td>
              <td>
                <div class="cell-stack">
                  <span class="num">{{ n(item.train_samples) }}</span>
                  <span class="cell-note">正样本 {{ n(item.positive_samples) }}</span>
                </div>
              </td>
              <td class="num">{{ metricOf(item, 'precision') }}</td>
              <td class="num">{{ missOf(item) }}</td>
              <td>
                <span v-if="item.artifact_path" class="cell-path num" :title="item.artifact_path">
                  {{ item.artifact_path }}
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td>
                <div class="cell-stack">
                  <span class="num">{{ stamp(item.promoted_at) }}</span>
                  <span class="cell-note num">训练 {{ stamp(item.trained_at) }}</span>
                  <span v-if="item.promoted_by" class="cell-note">操作人 {{ item.promoted_by }}</span>
                </div>
              </td>
              <td>
                <div class="row-actions">
                  <template v-if="item.status === 'candidate' && isAdmin">
                    <button
                      class="btn btn-sm"
                      :disabled="promoting === item.model_version"
                      @click="trialModel(item)"
                    >
                      {{ promoting === item.model_version ? '切换中…' : '试用' }}
                    </button>
                  </template>
                  <template v-else-if="item.status === 'trial' && isAdmin">
                    <button
                      class="btn btn-sm"
                      :disabled="promoting === item.model_version"
                      @click="confirmTrial(item)"
                    >
                      确认转正
                    </button>
                    <button
                      class="btn btn-sm btn-quiet"
                      :disabled="promoting === item.model_version"
                      @click="rollbackTrial(item)"
                    >
                      回退
                    </button>
                  </template>
                  <span v-else class="muted">—</span>
                </div>
              </td>
            </tr>
            <tr v-if="!models.length">
              <td colspan="9" class="empty">{{ modelsRequest.pending.value ? '正在读取模型版本…' : '暂无登记的模型版本' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager tight-pager"><span>离线指标来自训练报告，口径说明见页尾</span></div>
    </section>

    <section ref="driftSection" class="card">
      <div class="card-head">
        <span class="card-title">漂移事件</span>
        <span class="card-sub">
          共 <b class="num">{{ driftTotal }}</b> 条 · 指标越限时自动登记
          <template v-if="pickedDrift.size"> · 已选 <b class="num">{{ pickedDrift.size }}</b> 条</template>
        </span>
        <div v-if="pickedDrift.size" class="card-tools">
          <button class="btn btn-sm" :disabled="archivingDrift" @click="archivePickedDrift">
            {{ archivingDrift ? '归档中…' : `归档选中 ${pickedDrift.size} 条` }}
          </button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table dr-table">
          <colgroup>
            <col style="width: 38px" />
            <col style="width: 20%" />
            <col style="width: 11%" />
            <col style="width: 18%" />
            <col style="width: 15%" />
            <col style="width: 15%" />
            <col style="width: 21%" />
          </colgroup>
          <thead>
            <tr>
              <th class="pick-col">
                <input
                  type="checkbox"
                  class="pick"
                  :checked="allDriftPicked"
                  :disabled="!drift.length"
                  aria-label="全选本页"
                  @change="togglePickAllDrift"
                />
              </th>
              <th>时间</th>
              <th>层级</th>
              <th>信号</th>
              <th>观测值</th>
              <th>基线</th>
              <th>动作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in drift" :key="item.id" :class="{ 'is-picked': pickedDrift.has(item.id) }">
              <td class="pick-col">
                <input
                  type="checkbox"
                  class="pick"
                  :checked="pickedDrift.has(item.id)"
                  :aria-label="`选择 ${item.id}`"
                  @change="togglePickDrift(item.id)"
                />
              </td>
              <td class="num">{{ stamp(item.detected_at) }}</td>
              <td class="num" :title="levelHint(item.level)">{{ item.level }}</td>
              <td>{{ signalText(item.signal) }}</td>
              <td class="num">
                {{ n(item.observed, 3) }}
                <span class="cell-note">阈值 {{ n(item.threshold, 3) }}</span>
              </td>
              <td class="num">{{ n(item.baseline, 3) }}</td>
              <td>
                <StatusChip :tone="isTriggered(item) ? 'bad' : 'idle'" :label="actionText(item.action)" />
              </td>
            </tr>
            <tr v-if="!drift.length">
              <td colspan="7" class="empty">{{ driftRequest.pending.value ? '正在读取漂移事件…' : '没有未归档的漂移事件' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <Pager
        :page="driftPage"
        :total-pages="driftPages"
        :total="driftTotal"
        :page-size="DRIFT_PAGE_SIZE"
        note="观察级事件仅登记，归档只打标记"
        @update:page="driftPage = $event"
      />
    </section>

    <!-- 口径说明：原文一条不删，只是不占正文 -->
    <section class="card glossary">
      <button class="glossary-summary" :aria-expanded="glossaryOpen" @click="glossaryOpen = !glossaryOpen">
        <span class="card-title">统计口径与说明</span>
        <span class="card-sub">指标定义、分母、离线与线上差异 · 点击展开</span>
        <span class="glossary-toggle">{{ glossaryOpen ? '收起' : '展开' }}</span>
      </button>
      <div v-if="glossaryOpen" class="glossary-body">
        <p><b>样本量</b>：窗口结束后满 1 小时、按 1 小时真值规则标注的预测。</p>
        <p><b>故障提前叫出（批次命中率）</b>：1 小时内提前叫出的故障批次 ÷ 总故障批次；同一台设备同一次故障算一批，分母含提前量窗口。中位提前量取各批次提前分钟数的中位数。</p>
        <p><b>窗口漏报率 / 批次漏报率</b>：批次漏报率 = 1 − 批次命中率；窗口漏报率 = 1 − 召回率，两者口径不同，不可混用。</p>
        <p><b>窗口准确率</b>：(TP + TN) ÷ 全部窗口；窗口命中 {{ ratioOf(summary?.precision) }}、窗口漏报 {{ ratioOf(missRate) }}。</p>
        <p><b>每台设备日均误报</b>：误报窗口数 ÷ 设备数 ÷ 天数（固定取最近 1 天，与顶部统计窗口选择无关）；当前 {{ operations?.false_alarms ?? '—' }} 次误报窗口 ÷ {{ operations?.devices ?? '—' }} 台设备 ÷ {{ operations?.days ?? '—' }} 天。</p>
        <p><b>每台设备日均误报事件</b>：合并相邻高分窗口（间隔≤10分钟记一起）：{{ operations?.false_alarm_events ?? '—' }} 起误报事件，来自 {{ operations?.false_alarm_event_windows ?? '—' }} 个高分窗口（含命中真实故障的窗口，非纯误报窗口）；另有 {{ operations?.false_alarm_events_with_fault ?? '—' }} 起事件含真实故障、不计入误报（共 {{ operations?.false_alarm_event_total ?? '—' }} 起）。</p>
        <p><b>平均预警提前量</b>：中位数，90 分位 {{ operations?.lead_minutes_p90 ?? '—' }} 分钟，1 小时内提前发现 {{ operations?.captured ?? '—' }}/{{ operations?.faults ?? '—' }} 次故障。</p>
        <p><b>混淆矩阵</b>：按阈值判定，窗口口径（真值只看结束后 1 小时，分母含提前量窗口）。颜色深浅按对数刻度映射样本量。命中率与漏报率此消彼长：降低阈值可减少漏报，代价是误报增加。</p>
        <p><b>高风险组命中率</b>：累积增益曲线，横轴是抽查设备比例，纵轴是抓到故障比例；虚线为随机抽查基准。共 {{ riskBands?.samples ?? 0 }} 条样本、真实故障 {{ riskBands?.faults ?? 0 }} 次。</p>
        <p><b>按日趋势</b>：按故障批次统计，仅看 1 小时内是否被提前叫出；缺失日期不画点，也不跨日期连线。</p>
        <p><b>离线精确率 / 离线漏报率</b>：来自模型训练报告（多为合成数据），仅证明合成场景下的训练效果；真实运行效果以页面顶部按 1 小时真值统计的线上故障批次命中率与误报事件为准，二者口径不同、不可混用，也不代表实盘已验证。</p>
        <p><b>模型版本操作</b>：候选只能先试用，试用会实际切换并核对推理服务运行版本，试用期满后确认转正；退役与被拒版本仅作留档，不支持回退。</p>
        <p><b>版本一致性</b>：台账现役与推理服务实际加载版本不一致时，以推理服务实际运行版本为准，必要时检查 /health 是否反映最新加载；运行版本读不到时不作「已一致」判断。</p>
        <p><b>漂移事件</b>：观测值与基线采用同一口径；观察级事件仅登记，处置需人工确认。L1 分布漂移、L2 校准漂移、L3 精度退化，详见表内层级提示。</p>
      </div>
    </section>

    <!-- AI 解读大弹窗：居中、不占满屏，背后留出页面位置，读完知道自己在哪 -->
    <Teleport to="body">
      <div v-if="analysisOpen" class="qa-scrim" @click.self="closeAnalysis">
        <div class="qa-modal" role="dialog" aria-modal="true" aria-label="AI 解读详情">
          <div class="qa-head">
            <div class="qa-title">
              <b>AI 解读 · 漂移归因</b>
              <span v-if="driftAnalysis?.created_at" class="num">{{ stamp(driftAnalysis.created_at) }}</span>
            </div>
            <button class="qa-close" type="button" aria-label="关闭" @click="closeAnalysis">
              <Icon name="close" :size="16" />
            </button>
          </div>

          <div class="qa-body">
            <template v-if="driftAnalysis">
              <p class="qa-conclusion">{{ driftAnalysis.conclusion }}</p>

              <div v-if="driftAnalysis.reasons.length" class="qa-block">
                <h4>依据</h4>
                <ul>
                  <li v-for="(reason, index) in driftAnalysis.reasons" :key="index">{{ reason }}</li>
                </ul>
              </div>

              <div v-if="driftAnalysis.suggestions.length" class="qa-block">
                <h4>建议</h4>
                <ol>
                  <li v-for="(item, index) in driftAnalysis.suggestions" :key="index" class="qa-advice">{{ item }}</li>
                </ol>
              </div>

              <div v-if="trialAnalysis" class="qa-block is-trial">
                <h4>试用对比</h4>
                <p>{{ trialAnalysis.conclusion }}</p>
                <ul v-if="trialAnalysis.reasons.length">
                  <li v-for="(reason, index) in trialAnalysis.reasons" :key="index">{{ reason }}</li>
                </ul>
                <p v-for="(item, index) in trialAnalysis.suggestions" :key="`s${index}`" class="qa-advice">
                  建议：{{ item }}
                </p>
              </div>

              <p class="qa-meta">
                tokens {{ driftAnalysis.prompt_tokens + driftAnalysis.completion_tokens }} ·
                模型只给建议，不会自动切换版本
              </p>
            </template>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.qq {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.qq-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 14px;
  padding: 0 2px;
}

.qq-window {
  font-size: 11.5px;
  color: var(--ink-3);
}

.qq-window b {
  color: var(--ink-2);
}

.qq-refresh {
  margin-left: auto;
}

.qq-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

/* ---------- 首屏 A：质量速览 ---------- */
.hero {
  display: grid;
  /* 右侧核对区加宽到 300：左边读数与进度条就能贴在一起，中间不再空一大块 */
  grid-template-columns: minmax(0, 1fr) 300px;
}

.hero-main {
  min-width: 0;
  padding: 15px 20px 16px;
  /* 右侧核对区行数多，左侧读数少：垂直居中，别让下方空出一块 */
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.hero-eyebrow {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
}

.hero-eyebrow span {
  font-size: 11px;
  font-weight: 400;
  color: var(--ink-4);
}

/* 数字 + 格子 + 进度条三段式：文字只保留标签，读数靠长度和颜色表达 */
.hero-body {
  display: flex;
  /* 读数与进度条紧贴着排，条吃满剩余宽度：中间留白比条长更难看 */
  align-items: center;
  justify-content: flex-start;
  gap: 28px;
  margin-top: 6px;
}

.hero-lead {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.hero-value {
  font-size: 44px;
  font-weight: 700;
  letter-spacing: -0.035em;
  line-height: 1;
  color: var(--brand);
}

.hero-lead-side {
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
}

/* 一个方块 = 一个故障批次：绿色提前叫出，红色漏报 */
.batch-cells {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  /* 27 批一行放得下；超过 40 批才折行，避免首屏出现两排格子 */
  max-width: 300px;
}

.batch-cells i {
  width: 8px;
  height: 14px;
  border-radius: 2px;
  background: var(--track-bg);
}

.batch-cells i.is-hit {
  background: var(--ok);
}

.batch-cells i.is-miss {
  background: var(--bad);
}

.batch-caption {
  font-size: 11px;
  color: var(--ink-3);
  white-space: nowrap;
}

.hero-bars {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1 1 auto;
  min-width: 190px;
  max-width: 460px;
}

.mini {
  display: grid;
  grid-template-columns: 74px minmax(0, 1fr) 62px;
  align-items: center;
  gap: 8px;
}

.mini-label {
  font-size: 11px;
  color: var(--ink-3);
  white-space: nowrap;
}

.mini-track {
  height: 6px;
  border-radius: 999px;
  background: var(--track-bg);
  overflow: hidden;
}

.mini-track i {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 0.4s ease;
}

.mini-value {
  font-size: 12px;
  font-weight: 650;
  color: var(--ink);
  text-align: right;
  white-space: nowrap;
}

.hero-side {
  min-width: 0;
  padding: 15px 18px 14px;
  border-left: 1px solid var(--line);
  background: var(--card-soft);
  border-radius: 0 var(--r-lg) var(--r-lg) 0;
}

.side-label {
  font-size: 11px;
  color: var(--ink-3);
}

.side-version {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 6px 0 8px;
}

.side-version b {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
  word-break: break-all;
}

.side-warn,
.side-ok {
  margin: 0 0 10px;
  font-size: 11px;
  line-height: 1.6;
}

.side-warn {
  color: var(--bad-deep);
}

.side-warn.is-soft {
  color: var(--warn-deep);
}

.side-ok {
  color: var(--ok-deep);
}

.side-meta {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 3px 10px;
  padding-top: 9px;
  border-top: 1px solid var(--line);
  font-size: 10.5px;
}

.side-meta span {
  color: var(--ink-4);
  white-space: nowrap;
}

.side-meta b {
  min-width: 0;
  font-weight: 500;
  color: var(--ink-2);
  word-break: break-all;
}

/* ---------- 首屏 B：两列高信息区 ---------- */
.focus-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 1fr);
  gap: 14px;
  align-items: stretch;
}

.focus-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.focus-card .card-head {
  padding: 12px 16px 8px;
  gap: 8px;
}

.focus-card .card-sub {
  white-space: normal;
}

.insight {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
  min-height: 0;
  padding: 0 16px 12px;
}

.insight-conclusion {
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.6;
  color: var(--ink);
}

.insight-list {
  margin: 6px 0 0;
  padding-left: 16px;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink-2);
}

/* 结论依旧限两行；其余内容全在弹窗里 */
.clamp-2 {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}

.insight-peek {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.insight-open {
  margin-left: auto;
  font-weight: 550;
  color: var(--brand);
  white-space: nowrap;
}

.insight.is-clickable {
  cursor: pointer;
  border-radius: var(--r-md);
  transition: background 0.16s ease;
}

.insight.is-clickable:hover {
  background: var(--card-soft);
}

.insight.is-clickable:hover .insight-open {
  color: var(--brand-deep);
}

/* ---------- AI 解读大弹窗 ---------- */
.qa-scrim {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: grid;
  place-items: center;
  padding: 44px 24px;
  background: var(--overlay);
  animation: qa-fade 0.16s ease;
}

@keyframes qa-fade {
  from {
    opacity: 0;
  }
}

.qa-modal {
  width: min(760px, 92vw);
  max-height: min(76vh, 720px);
  display: flex;
  flex-direction: column;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-3);
  overflow: hidden;
}

.qa-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 13px 18px;
  border-bottom: 1px solid var(--line);
}

.qa-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}

.qa-title b {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--ink);
}

.qa-title span {
  font-size: 11.5px;
  color: var(--ink-4);
}

.qa-close {
  margin-left: auto;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: var(--r-sm);
  color: var(--ink-3);
  transition: background 0.14s ease, color 0.14s ease;
}

.qa-close:hover {
  background: var(--hover-bg);
  color: var(--ink);
}

.qa-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 20px 20px;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ink-2);
}

.qa-conclusion {
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.65;
  color: var(--ink);
}

.qa-block {
  margin-top: 18px;
}

.qa-block h4 {
  margin-bottom: 7px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--ink-4);
}

.qa-block ul,
.qa-block ol {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.qa-block li {
  padding-left: 2px;
}

.qa-block.is-trial {
  padding: 12px 14px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
}

.qa-advice {
  color: var(--brand);
}

.qa-meta {
  margin-top: 20px;
  padding-top: 10px;
  border-top: 1px solid var(--line-soft);
  font-size: 11px;
  color: var(--ink-4);
}

.insight-empty {
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--ink-3);
}

/* 试用对比单独成块：两块文字用底色分开，扫读时不会连成一大段 */
.insight-trial {
  margin-top: 9px;
  padding: 8px 10px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
}

.retrain-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 9px 16px 11px;
  border-top: 1px solid var(--line-soft);
}

.retrain-text {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.retrain-text b {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--ink);
}

.retrain-note {
  font-size: 10.5px;
  line-height: 1.5;
  color: var(--ink-3);
}

.retrain-note.is-ok {
  color: var(--ok-deep);
}

.retrain-note.is-blocked {
  color: var(--warn-deep);
}

.version-panel {
  padding: 0 16px 10px;
}

.version-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 5px 0;
  border-bottom: 1px solid var(--line-soft);
}

.version-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--ink);
}

.version-meta {
  font-size: 10.5px;
  color: var(--ink-3);
}

.version-warn {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.55;
  color: var(--bad-deep);
}

.version-warn.is-soft {
  color: var(--warn-deep);
}

.link-btn {
  margin-top: 7px;
  padding: 0;
  border: 0;
  background: none;
  font-size: 11px;
  color: var(--brand);
  cursor: pointer;
}

.link-btn:hover {
  text-decoration: underline;
}

.drift-panel {
  flex: 1;
  padding: 9px 16px 4px;
  border-top: 1px solid var(--line-soft);
}

.drift-panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}

.drift-panel-head b {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--ink);
}

.drift-panel-head span {
  font-size: 10.5px;
  color: var(--ink-3);
}

.drift-list {
  display: flex;
  flex-direction: column;
}

.drift-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px dashed var(--line-soft);
}

.drift-item:last-child {
  border-bottom: 0;
}

.drift-main {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.drift-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--ink);
}

.drift-title b {
  margin-right: 5px;
  font-size: 10.5px;
  color: var(--ink-3);
}

.drift-value,
.drift-time {
  font-size: 10.5px;
  color: var(--ink-3);
}

.drift-empty {
  padding: 8px 0;
  font-size: 11.5px;
  color: var(--ink-3);
}

.focus-foot {
  margin: 0 16px 11px;
  text-align: left;
}

/* ---------- 下滑区 ---------- */
.section-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  padding: 6px 2px 0;
}

.section-title h2 {
  font-size: 13px;
  font-weight: 600;
  color: var(--ink);
}

.section-title span {
  font-size: 11px;
  color: var(--ink-4);
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
  align-items: stretch;
}

.detail-grid > .card,
.trend-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.detail-grid .card-head,
.trend-card .card-head {
  padding: 11px 16px 8px;
}

.cm-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0 16px 12px;
}

.cm-note {
  margin-top: 6px;
  font-size: 10.5px;
  line-height: 1.55;
  color: var(--ink-4);
}

.bands-loading {
  padding: 12px 16px 18px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.md-table {
  table-layout: fixed;
  min-width: 900px;
}

.dr-table {
  table-layout: fixed;
  min-width: 760px;
}

/* 勾选列：原生 checkbox + accent-color，深浅主题都跟着品牌色走 */
.dr-table th.pick-col,
.dr-table td.pick-col {
  padding-left: 14px;
  padding-right: 0;
}

.pick {
  width: 14px;
  height: 14px;
  accent-color: var(--brand);
  cursor: pointer;
  vertical-align: middle;
}

.md-table .cell-note,
.dr-table .cell-note {
  font-size: 10.5px;
  color: var(--ink-3);
}

.cell-path {
  display: block;
  font-size: 10.5px;
  line-height: 1.45;
  color: var(--ink-3);
  word-break: break-all;
}

.tight-pager {
  padding: 9px 20px;
  font-size: 11px;
}

/* 口径说明：默认收起，长文案一条不删 */
.glossary {
  padding: 0;
}

.glossary-summary {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 18px;
  background: none;
  border: 0;
  text-align: left;
  cursor: pointer;
}

.glossary-toggle {
  margin-left: auto;
  font-size: 11px;
  color: var(--brand);
}

.glossary-body {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 24px;
  padding: 4px 18px 16px;
  border-top: 1px solid var(--line-soft);
}

.glossary-body p {
  font-size: 11px;
  line-height: 1.7;
  color: var(--ink-3);
}

.glossary-body b {
  font-weight: 600;
  color: var(--ink-2);
}

@media (max-width: 1180px) {
  .hero {
    grid-template-columns: minmax(0, 1fr);
  }

  .hero-side {
    border-left: 0;
    border-top: 1px solid var(--line);
    border-radius: 0 0 var(--r-lg) var(--r-lg);
  }

  .focus-grid,
  .detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .glossary-body {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 760px) {
  .hero-value {
    font-size: 36px;
  }

  .hero-main,
  .hero-side {
    padding: 13px 14px;
  }

  .qq-refresh {
    margin-left: 0;
  }

  .fact {
    white-space: normal;
  }
}
</style>
