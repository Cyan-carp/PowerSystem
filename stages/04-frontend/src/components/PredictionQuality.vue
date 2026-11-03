<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import StatusChip from './StatusChip.vue'
import RiskBandsChart from './RiskBandsChart.vue'
import ConfusionMatrix from './ConfusionMatrix.vue'
import QualityTrend from './QualityTrend.vue'
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

/**
 * 运维视角单独一路请求：它只是补充口径，失败了也不该把页面顶部的错误条拉出来，
 * 更不能挡住主 KPI 的展示，所以不进 failed 汇总。
 */
const operationsRequest = useLatestRequest<QualityOperations>()
const operations = computed(() => operationsRequest.data.value)

const summary = computed(() => summaryRequest.data.value)
const points = computed(() => seriesRequest.data.value?.points ?? [])
const riskBands = computed(() => riskBandsRequest.data.value)
const models = computed(() => modelsRequest.data.value?.items ?? [])
/** 推理服务实际加载的版本；与台账现役不一致时必须提示，否则会静默跑错模型 */
const runtimeVersion = computed(() => modelsRequest.data.value?.runtime_version ?? '')
const ledgerActive = computed(() => models.value.find((item) => item.status === 'active')?.model_version ?? '')
const versionMismatch = computed(
  () => Boolean(runtimeVersion.value && ledgerActive.value && runtimeVersion.value !== ledgerActive.value),
)
const drift = computed(() => driftRequest.data.value ?? [])

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

const loadCore = () => {
  const to = new Date()
  const from = new Date(to.getTime() - days.value * DAY)
  const params = { from: from.toISOString(), to: to.toISOString() }
  range.value = params
  void summaryRequest.run((signal) => api.predictionQualitySummary(params, signal))
  void seriesRequest.run((signal) => api.predictionQualitySeries(days.value, signal))
  void riskBandsRequest.run((signal) => api.predictionQualityRiskBands(days.value, signal))
}

/** 版本与漂移不随窗口变化，进页面拉一次即可；重拉时保留旧行，避免整表先闪成空 */
const loadModels = () =>
  modelsRequest.run(async (signal) => await api.predictionQualityModels(signal), { keepData: true })
const loadDrift = () =>
  driftRequest.run(async (signal) => (await api.predictionQualityDrift(50, signal)).items, { keepData: true })
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

/* ---------------- 指标文案 ---------------- */

/** 比例一律转百分比；null 是「算不出来」，显示「—」*/
const ratioOf = (value: number | null | undefined) => (value == null ? '—' : pct(value * 100, 1))

/** 漏报率 = 1 − 召回率：模型没抓到的真实故障占比 */
const missRate = computed(() => {
  const recall = summary.value?.recall
  return recall == null ? null : 1 - recall
})

const kpis = computed(() => {
  const value = summary.value
  return [
    {
      key: 'samples',
      label: '样本量',
      color: 'var(--brand)',
      value: value ? n(value.samples) : '—',
      note: '窗口已结算且有真值标注的预测',
    },
    { key: 'precision', label: '命中率', color: 'var(--ok)', value: value ? ratioOf(value.precision) : '—', note: 'precision = TP ÷ (TP + FP)' },
    { key: 'miss', label: '漏报率', color: 'var(--bad)', value: value ? ratioOf(missRate.value) : '—', note: '1 − recall = FN ÷ (TP + FN)' },
    { key: 'accuracy', label: '准确率', color: 'var(--ink-4)', value: value ? ratioOf(value.accuracy) : '—', note: '(TP + TN) ÷ 全部样本' },
  ]
})

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

const promote = async (item: PredictionModelVersion) => {
  const target = item.model_version
  const ok = await askConfirm({
    title: `把 ${target} 提升为现役`,
    body: '确认后仅变更登记的现役版本；模型文件由运维更新，历史预测不重算。',
    confirmText: '提升为现役',
  })
  if (!ok) return
  promoting.value = target
  try {
    const result = await api.promoteModel(target)
    toast('ok', `${result.model_version} 已登记为${statusOf(result.status).label}`)
    await loadModels()
  } catch (error) {
    toast('bad', `提升失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    promoting.value = ''
  }
}

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
/** 越线触发处置的才算醒目事件；只登记观察的保持中性色 */
const isTriggered = (item: DriftEvent) => item.action === 'triggered' || item.level === 'triggered'
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
        <template v-if="summary?.model_version"> · 现役版本 <b class="num">{{ summary.model_version }}</b></template>
      </span>
      <button class="btn btn-sm qq-refresh" :disabled="loading" @click="reload">
        <Icon name="refresh" :size="13" />刷新
      </button>
    </div>

    <div v-if="failed" class="error-box qq-error" role="alert">
      <span>模型质量数据未取到：{{ failed }}</span>
      <button class="btn btn-sm" @click="reload">重试</button>
    </div>

    <section class="card strip">
      <div v-for="item in kpis" :key="item.key" class="strip-cell">
        <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
        <span class="strip-value num">{{ item.value }}</span>
        <span class="strip-note">{{ item.note }}</span>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">运维视角</span>
        <span class="card-sub">同一批数据，按设备摊薄后的口径</span>
      </div>
      <div class="ov-grid">
        <div class="ov-cell">
          <span class="ov-label"><i :style="{ background: 'var(--brand)' }"></i>每台设备日均误报</span>
          <span v-if="operations" class="ov-value num">
            {{ operations.false_alarms_per_device_day.toFixed(1) }}<span class="ov-unit">次</span>
          </span>
          <span v-else class="ov-value num">—</span>
          <span v-if="operations" class="ov-note">
            {{ operations.false_alarms }} 次误报 ÷ {{ operations.devices }} 台设备 ÷ {{ operations.days }} 天
          </span>
        </div>
        <div class="ov-cell">
          <span class="ov-label"><i :style="{ background: 'var(--ok)' }"></i>平均预警提前量</span>
          <span v-if="operations" class="ov-value num">
            {{ operations.lead_minutes_p50 }}<span class="ov-unit">分钟</span>
          </span>
          <span v-else class="ov-value num">—</span>
          <span v-if="operations" class="ov-note">
            中位数 · 90 分位 {{ operations.lead_minutes_p90 }} 分钟 · {{ operations.captured }}/{{ operations.faults }} 次故障被提前发现
          </span>
        </div>
      </div>
    </section>

    <div class="qq-grid">
      <section class="card">
        <div class="card-head">
          <span class="card-title">混淆矩阵</span>
          <span class="card-sub">按阈值判定的预测结果</span>
        </div>
        <div class="cm-wrap">
          <ConfusionMatrix
            :tp="summary?.tp ?? 0"
            :fp="summary?.fp ?? 0"
            :fn="summary?.fn ?? 0"
            :tn="summary?.tn ?? 0"
          />
          <p class="cm-note">
            颜色深浅按对数刻度映射样本量。命中率与漏报率此消彼长：降低阈值可减少漏报，代价是误报增加。
          </p>
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
          :bands="riskBands?.items ?? []"
          :samples="riskBands?.samples ?? 0"
          :faults="riskBands?.faults ?? 0"
          :baseline-rate="riskBands?.baseline_rate ?? 0"
        />
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <span class="card-title">按日趋势</span>
        <span class="card-sub">近 {{ days }} 天 · 命中率与漏报率</span>
      </div>
      <QualityTrend :points="points" :pending="seriesRequest.pending.value" />
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">模型运营</span>
        <span class="card-sub">重训与解读 · 只给建议，不自动执行</span>
      </div>
      <div class="ops">
        <div class="ops-block">
          <div class="ops-head">
            <span class="ops-title">用新数据重训</span>
            <button
              class="btn btn-sm"
              :disabled="!eligibility?.ready || training"
              :title="eligibility?.ready ? '按最近 120 天窗口模式数据重训一份候选' : (eligibility?.blockers.join('；') || '正在读取数据量')"
              @click="startTrain"
            >
              {{ training ? '训练中…' : '开始重训' }}
            </button>
          </div>
          <p v-if="eligibility" class="ops-meta">
            已积累 <b class="num">{{ eligibility.days }}</b> 天 ·
            <b class="num">{{ eligibility.samples }}</b> 条样本 ·
            <b class="num">{{ eligibility.positives }}</b> 个正样本
          </p>
          <p v-if="eligibility" class="ops-note-line" :class="eligibility.ready ? 'is-ok' : 'is-blocked'">
            {{ eligibility.ready ? '已达准入门槛，可以重训' : eligibility.blockers.join('；') }}
          </p>
        </div>

        <div class="ops-block">
          <div class="ops-head">
            <span class="ops-title">AI 解读</span>
            <div class="ops-actions">
              <button class="btn btn-sm" :disabled="analyzing !== ''" @click="runAnalysis('drift')">
                {{ analyzing === 'drift' ? '分析中…' : '分析漂移' }}
              </button>
              <button class="btn btn-sm btn-quiet" :disabled="analyzing !== ''" @click="runAnalysis('trial')">
                {{ analyzing === 'trial' ? '分析中…' : '对比试用' }}
              </button>
            </div>
          </div>
          <div v-if="driftAnalysis" class="ops-result">
            <p class="ops-conclusion">{{ driftAnalysis.conclusion }}</p>
            <ul v-if="driftAnalysis.reasons.length" class="ops-list">
              <li v-for="reason in driftAnalysis.reasons" :key="reason">{{ reason }}</li>
            </ul>
            <p v-if="driftAnalysis.suggestions.length" class="ops-suggest">建议：{{ driftAnalysis.suggestions.join('；') }}</p>
            <p class="ops-tokens">
              {{ driftAnalysis.created_at ? stamp(driftAnalysis.created_at) : '' }} ·
              tokens {{ driftAnalysis.prompt_tokens + driftAnalysis.completion_tokens }}
            </p>
          </div>
          <p v-else class="ops-empty">还没有解读记录，点「分析漂移」让模型读一遍分时误差</p>
          <div v-if="trialAnalysis" class="ops-result ops-result-trial">
            <p class="ops-conclusion">试用对比：{{ trialAnalysis.conclusion }}</p>
            <p v-if="trialAnalysis.suggestions.length" class="ops-suggest">建议：{{ trialAnalysis.suggestions.join('；') }}</p>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">模型版本</span>
        <span class="card-sub">{{ models.length }} 个版本 · 候选可试用，试用期满可转正</span>
      </div>
      <p v-if="versionMismatch" class="model-warn" role="alert">
        台账现役为 <b class="num">{{ ledgerActive }}</b>，推理服务实际加载的是 <b class="num">{{ runtimeVersion }}</b>。
        提升只改台账，需要运维替换模型文件并重启推理服务后才会一致。
      </p>
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
              <th>命中率</th>
              <th>漏报率</th>
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
                    <button
                      class="btn btn-sm btn-quiet"
                      :disabled="promoting === item.model_version"
                      @click="promote(item)"
                    >
                      直接提升
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
      <div class="pager">
        <span>提升仅变更登记的现役版本，模型文件由运维更新；退役与被拒版本仅作留档，不支持回退。</span>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">漂移事件</span>
        <span class="card-sub">最近 {{ drift.length }} 条 · 指标越限时自动登记</span>
      </div>
      <div class="table-wrap">
        <table class="table dr-table">
          <colgroup>
            <col style="width: 20%" />
            <col style="width: 11%" />
            <col style="width: 18%" />
            <col style="width: 15%" />
            <col style="width: 15%" />
            <col style="width: 21%" />
          </colgroup>
          <thead>
            <tr>
              <th>时间</th>
              <th>层级</th>
              <th>信号</th>
              <th>观测值</th>
              <th>基线</th>
              <th>动作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in drift" :key="item.id">
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
              <td colspan="6" class="empty">{{ driftRequest.pending.value ? '正在读取漂移事件…' : '窗口内无漂移事件' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>观测值与基线采用同一口径；观察级事件仅登记，处置需人工确认。</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
.qq {
  display: flex;
  flex-direction: column;
  gap: 16px;
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

/* KPI 四格：与预测排行页的 strip 同一套视觉，切页签时行高不跳 */
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

/* 运维视角：两格并排，与 KPI 条同一套「左分隔线 + 大数字」的节奏，一眼能读 */
.ov-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  padding: 2px 0 6px;
}

.ov-cell {
  padding: 10px 20px 12px;
  border-left: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.ov-cell:first-child {
  border-left: 0;
}

.ov-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.ov-label i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.ov-value {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.3;
  color: var(--ink);
}

.ov-unit {
  margin-left: 3px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-3);
}

.ov-note {
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--ink-4);
}

.qq-grid {
  display: grid;
  /* 左列固定宽：混淆矩阵是 2x2 方阵，跟着右列等比拉宽会变成扁长条 */
  /* 1:1 等分：混淆矩阵压缩后不再需要固定宽度，两张卡片等高更整齐 */
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

/* 两张卡等高；内容在卡内垂直居中，避免底部留出大片空白 */
.qq-grid > .card {
  display: flex;
  flex-direction: column;
}

/* ---- 混淆矩阵 ---- */
.cm-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0 18px 14px;
}

/* 台账与运行态不一致时必须显眼：提完不换文件会静默跑旧模型 */
.train-hint {
  margin: 0 18px 10px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

/* 操作列：试用/提升、确认/回退各一组，窄屏时会换行 */
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

/* 模型运营：左重训、右解读 */
.ops {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr);
  gap: 18px;
  padding: 14px 18px 18px;
}

.ops-block {
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--card-soft, var(--card));
}

.ops-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.ops-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--ink);
}

.ops-actions {
  display: flex;
  gap: 6px;
}

.ops-meta {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--ink-2);
}

.ops-note-line {
  margin: 6px 0 0;
  font-size: 11px;
  line-height: 1.6;
}

.ops-note-line.is-ok {
  color: var(--ok);
}

.ops-note-line.is-blocked {
  color: var(--warn);
}

.ops-result {
  margin-top: 8px;
}

.ops-result-trial {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}

.ops-conclusion {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.65;
  color: var(--ink);
}

.ops-list {
  margin: 6px 0 0;
  padding-left: 16px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--ink-2);
}

.ops-suggest {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--brand);
}

.ops-tokens {
  margin: 6px 0 0;
  font-size: 10.5px;
  color: var(--ink-3);
}

.ops-empty {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--ink-3);
}

@media (max-width: 1180px) {
  .ops {
    grid-template-columns: minmax(0, 1fr);
  }
}

.model-warn {
  margin: 0 18px 10px;
  padding: 9px 12px;
  border: 1px solid var(--warn);
  border-radius: var(--r-md);
  background: var(--warn-soft);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink);
}

.model-warn b {
  font-weight: 700;
}

.cell-path {
  display: block;
  font-size: 10.5px;
  line-height: 1.45;
  color: var(--ink-3);
  word-break: break-all;
}

.cm-note {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--ink-3);
}

/* ---- 两张表 ---- */
.md-table {
  table-layout: fixed;
  min-width: 900px;
}

.dr-table {
  table-layout: fixed;
  min-width: 720px;
}

/* 表格里的第二行说明统一用小字，避免整列被撑高 */
.md-table .cell-note,
.dr-table .cell-note {
  font-size: 10.5px;
  color: var(--ink-3);
}

@media (max-width: 1240px) {
  .qq-grid {
    grid-template-columns: minmax(0, 1fr);
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

  /* 窄屏两格改成上下排，分隔线从左侧换成上方 */
  .ov-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .ov-cell {
    border-left: 0;
  }

  .ov-cell + .ov-cell {
    border-top: 1px solid var(--line-soft);
  }

  .qq-refresh {
    margin-left: 0;
  }
}
</style>
