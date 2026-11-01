<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import StatusChip from './StatusChip.vue'
import CalibrationChart from './CalibrationChart.vue'
import QualityTrend from './QualityTrend.vue'
import { api } from '../lib/api'
import type {
  DriftEvent,
  PredictionModelVersion,
  PredictionQualityCalibration,
  PredictionQualitySeries,
  PredictionQualitySummary,
} from '../lib/api'
import { isAdmin } from '../lib/auth'
import { n, pct, stamp } from '../lib/format'
import { askConfirm, toast } from '../lib/toast'
import { useLatestRequest } from '../lib/useLatestRequest'

/**
 * 预测质量：只看「已经到期、有真实结果」的预测。
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
const calibrationRequest = useLatestRequest<PredictionQualityCalibration>()
const modelsRequest = useLatestRequest<PredictionModelVersion[]>()
const driftRequest = useLatestRequest<DriftEvent[]>()

const summary = computed(() => summaryRequest.data.value)
const points = computed(() => seriesRequest.data.value?.points ?? [])
const calibration = computed(() => calibrationRequest.data.value)
const models = computed(() => modelsRequest.data.value ?? [])
const drift = computed(() => driftRequest.data.value ?? [])

const loading = computed(
  () => summaryRequest.pending.value || seriesRequest.pending.value || calibrationRequest.pending.value,
)

/** 失败按区块列出来，全部拼成一行：错误最多一行，重试一次全拉 */
const failed = computed(() =>
  [
    { label: '质量汇总', error: summaryRequest.error.value },
    { label: '按天趋势', error: seriesRequest.error.value },
    { label: '校准曲线', error: calibrationRequest.error.value },
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
  void calibrationRequest.run((signal) => api.predictionQualityCalibration(days.value, signal))
}

/** 版本与漂移不随窗口变化，进页面拉一次即可；重拉时保留旧行，避免整表先闪成空 */
const loadModels = () =>
  modelsRequest.run(async (signal) => (await api.predictionQualityModels(signal)).items, { keepData: true })
const loadDrift = () =>
  driftRequest.run(async (signal) => (await api.predictionQualityDrift(50, signal)).items, { keepData: true })

const reload = () => {
  loadCore()
  void loadModels()
  void loadDrift()
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
      label: '样本数',
      color: 'var(--brand)',
      value: value ? n(value.samples) : '—',
      note: '窗口内已到期、有真实结果的预测',
    },
    { key: 'precision', label: '命中率', color: 'var(--ok)', value: value ? ratioOf(value.precision) : '—', note: 'precision = TP ÷ (TP + FP)' },
    { key: 'miss', label: '漏报率', color: 'var(--bad)', value: value ? ratioOf(missRate.value) : '—', note: '1 − recall = FN ÷ (TP + FN)' },
    { key: 'accuracy', label: '准确率', color: 'var(--ink-4)', value: value ? ratioOf(value.accuracy) : '—', note: '(TP + TN) ÷ 全部样本' },
  ]
})

const cellText = (key: 'tp' | 'fp' | 'fn' | 'tn') => (summary.value ? n(summary.value[key]) : '—')

/* ---------------- 模型版本 ---------------- */

const MODEL_STATUS: Record<string, { label: string; tone: string }> = {
  active: { label: '现役', tone: 'ok' },
  candidate: { label: '候选', tone: 'brand' },
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
    body: '确认后只切换平台登记的现役版本，真实模型文件由运维替换；已经产生的预测不会被重算。',
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
      <span>预测质量数据未取到：{{ failed }}</span>
      <button class="btn btn-sm" @click="reload">重试</button>
    </div>

    <section class="card strip">
      <div v-for="item in kpis" :key="item.key" class="strip-cell">
        <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
        <span class="strip-value num">{{ item.value }}</span>
        <span class="strip-note">{{ item.note }}</span>
      </div>
    </section>

    <div class="qq-grid">
      <section class="card">
        <div class="card-head">
          <span class="card-title">混淆矩阵</span>
          <span class="card-sub">概率达阈值判为故障</span>
        </div>
        <div class="cm-wrap">
          <table class="cm">
            <thead>
              <tr>
                <th class="cm-corner"></th>
                <th>预测故障</th>
                <th>预测正常</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th>实际故障</th>
                <td>
                  <div class="cm-cell is-hit">
                    <b class="num">{{ cellText('tp') }}</b>
                    <span>命中</span>
                    <em>确实故障</em>
                  </div>
                </td>
                <td>
                  <div class="cm-cell is-miss">
                    <b class="num">{{ cellText('fn') }}</b>
                    <span>漏报</span>
                    <em>故障被放过</em>
                  </div>
                </td>
              </tr>
              <tr>
                <th>实际正常</th>
                <td>
                  <div class="cm-cell is-false">
                    <b class="num">{{ cellText('fp') }}</b>
                    <span>误报</span>
                    <em>白跑一趟</em>
                  </div>
                </td>
                <td>
                  <div class="cm-cell is-safe">
                    <b class="num">{{ cellText('tn') }}</b>
                    <span>正确拒绝</span>
                    <em>确实正常</em>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="cm-note">
            命中与漏报此消彼长：把阈值调低能捞回漏报，代价是误报变多。四格都看，才不会被单看命中率误导。
          </p>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">校准曲线</span>
          <span class="card-sub">平均预测概率 {{ ratioOf(summary?.prob_mean) }} · 高风险占比 {{ ratioOf(summary?.high_ratio) }}</span>
        </div>
        <CalibrationChart
          :buckets="calibration?.buckets ?? []"
          :ece="calibration?.ece ?? null"
          :pending="calibrationRequest.pending.value"
        />
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <span class="card-title">按天趋势</span>
        <span class="card-sub">近 {{ days }} 天 · 命中率与漏报率</span>
      </div>
      <QualityTrend :points="points" :pending="seriesRequest.pending.value" />
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">模型版本</span>
        <span class="card-sub">{{ models.length }} 个版本 · 只有候选版本可提升</span>
      </div>
      <div class="table-wrap">
        <table class="table md-table">
          <colgroup>
            <col style="width: 17%" />
            <col style="width: 13%" />
            <col style="width: 13%" />
            <col style="width: 15%" />
            <col style="width: 9%" />
            <col style="width: 9%" />
            <col style="width: 14%" />
            <col style="width: 10%" />
          </colgroup>
          <thead>
            <tr>
              <th>版本</th>
              <th>状态</th>
              <th>数据来源</th>
              <th>训练样本</th>
              <th>命中率</th>
              <th>漏报率</th>
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
                <div class="cell-stack">
                  <span class="num">{{ stamp(item.promoted_at) }}</span>
                  <span class="cell-note num">训练 {{ stamp(item.trained_at) }}</span>
                </div>
              </td>
              <td>
                <button
                  v-if="item.status === 'candidate' && isAdmin"
                  class="btn btn-sm"
                  :disabled="promoting === item.model_version"
                  @click="promote(item)"
                >
                  {{ promoting === item.model_version ? '提升中…' : '提升为现役' }}
                </button>
                <span v-else class="muted">—</span>
              </td>
            </tr>
            <tr v-if="!models.length">
              <td colspan="8" class="empty">{{ modelsRequest.pending.value ? '正在读取模型版本…' : '暂无登记的模型版本' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>提升只改平台登记的现役版本，模型文件由运维替换；退役与被拒版本只作留档，不能回切。</span>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">漂移事件</span>
        <span class="card-sub">最近 {{ drift.length }} 条 · 指标越线时自动登记</span>
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
              <td colspan="6" class="empty">{{ driftRequest.pending.value ? '正在读取漂移事件…' : '窗口内没有漂移事件' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>观测值与基线按同一口径记录；「观察」只登记不处置，处置动作仍需人工确认。</span>
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

.qq-grid {
  display: grid;
  grid-template-columns: minmax(0, 330px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

/* ---- 混淆矩阵 ---- */
.cm-wrap {
  padding: 0 18px 16px;
}

.cm {
  width: 100%;
  border-collapse: separate;
  border-spacing: 6px;
}

.cm th {
  font-size: 11px;
  font-weight: 500;
  color: var(--ink-3);
  text-align: center;
  padding: 2px 0;
  white-space: nowrap;
}

.cm tbody th {
  text-align: left;
  padding-right: 2px;
}

.cm-corner {
  width: 44px;
}

.cm-cell {
  display: flex;
  flex-direction: column;
  gap: 1px;
  align-items: center;
  padding: 11px 6px 9px;
  border-radius: var(--r-md);
  border: 1px solid var(--line-soft);
}

.cm-cell b {
  font-size: 19px;
  font-weight: 700;
  line-height: 1.2;
  color: var(--ink);
}

.cm-cell span {
  font-size: 11.5px;
  color: var(--ink-2);
}

.cm-cell em {
  font-style: normal;
  font-size: 10.5px;
  color: var(--ink-3);
}

/* 命中是「模型做对了」，用品牌色淡底；其余三格沿用语义浅底，不加重色块 */
.cm-cell.is-hit {
  background: var(--brand-soft);
}

.cm-cell.is-miss {
  background: var(--bad-soft);
}

.cm-cell.is-false {
  background: var(--warn-soft);
}

.cm-cell.is-safe {
  background: var(--ok-soft);
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

  .qq-refresh {
    margin-left: 0;
  }
}
</style>
