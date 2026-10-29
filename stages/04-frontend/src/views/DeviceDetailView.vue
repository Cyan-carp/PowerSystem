<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import PowerChart from '../components/PowerChart.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import type { Alarm, Device, HealthRow, Metric, Prediction, Telemetry } from '../lib/api'
import { FEATURE_CN, FEATURE_UNIT } from '../lib/data'
import { shell } from '../lib/store'
import { across, HEALTH_STATE_CN, healthBasis, LEVEL_CN, METRIC_CN, METRIC_DIGITS, METRIC_UNIT, n, pct, stamp, TELEMETRY_STATE_CN, toneOf } from '../lib/format'

const route = useRoute()
const router = useRouter()

const device = ref<Device | null>(null)
const latest = ref<Telemetry | null>(null)
const health = ref<HealthRow | null>(null)
const healthComputed = ref('')
const prediction = ref<Prediction | null>(null)
const alarms = ref<Alarm[]>([])
const points = ref<[number, number][]>([])
const loading = ref(true)
const chartLoading = ref(false)
const exporting = ref(false)
const failed = ref('')
const chartError = ref('')
const exportError = ref('')

const deviceID = computed(() => Number(route.params.id))

/* ---------------- 曲线查询 ---------------- */

const metric = ref<Metric>('temperature')
const preset = ref(1)
const startText = ref('')
const endText = ref('')

const toLocalInput = (date: Date) => {
  const beijing = new Date(date.getTime() + 8 * 3_600_000)
  return beijing.toISOString().slice(0, 16)
}

const applyPreset = (hours: number) => {
  preset.value = hours
  const end = new Date()
  startText.value = toLocalInput(new Date(end.getTime() - hours * 3_600_000))
  endText.value = toLocalInput(end)
  void loadChart()
}

/** 前端按北京时间（+08:00）解释输入，避免浏览器本地时区把窗口挪走 */
const parseInput = (text: string) => {
  const value = Date.parse(`${text}:00+08:00`)
  return Number.isFinite(value) ? new Date(value) : null
}

/* 命名避开全局 window：同名会遮蔽 window.setInterval 一类的浏览器 API */
const range = computed(() => {
  const start = parseInput(startText.value)
  const end = parseInput(endText.value)
  if (!start || !end || end.getTime() <= start.getTime()) return null
  if (end.getTime() - start.getTime() > 24 * 3_600_000) return null
  return { start, end }
})

/** 真实后端单次最多 5000 点，24h / 5s 采样必然超限；按 6h 分段取再合并。 */
const CHUNK_MS = 6 * 3_600_000

const loadChart = async () => {
  if (!device.value) return
  if (!range.value) {
    chartError.value = '时间范围需在 24 小时以内，且结束时间晚于开始时间'
    points.value = []
    return
  }
  chartError.value = ''
  chartLoading.value = true
  const { start, end } = range.value
  const merged = new Map<number, number>()
  const jobs: Promise<void>[] = []
  for (let from = start.getTime(); from < end.getTime(); from += CHUNK_MS) {
    const to = Math.min(end.getTime(), from + CHUNK_MS)
    jobs.push(
      api.history(device.value.id, metric.value, new Date(from), new Date(to)).then((payload) => {
        for (const [ts, value] of payload.points) merged.set(ts, value)
      }),
    )
  }
  try {
    const results = await Promise.allSettled(jobs)
    if (results.every((item) => item.status === 'rejected')) {
      const reason = results[0]?.status === 'rejected' ? String((results[0] as PromiseRejectedResult).reason) : ''
      throw new Error(reason || '历史遥测不可用')
    }
    points.value = [...merged.entries()].sort((a, b) => a[0] - b[0])
  } catch (error) {
    chartError.value = `历史遥测未取到：${String(error)}`
    points.value = []
  } finally {
    chartLoading.value = false
  }
}

const exportCsv = async () => {
  exportError.value = ''
  if (!device.value || !range.value) {
    exportError.value = '请先选择有效的查询时间范围'
    return
  }
  exporting.value = true
  try {
    await api.exportTelemetry(device.value.id, range.value.start, range.value.end)
  } catch (error) {
    exportError.value = `导出失败：${String(error)}`
  } finally {
    exporting.value = false
  }
}

/* ---------------- 档案、健康、预测、告警 ---------------- */

const loadAll = async () => {
  loading.value = true
  failed.value = ''
  const id = deviceID.value
  if (!Number.isInteger(id) || id < 1) {
    failed.value = '设备编号无效'
    loading.value = false
    return
  }
  try {
    device.value = await api.device(id)
  } catch (error) {
    failed.value = `设备档案未取到：${String(error)}`
    loading.value = false
    return
  }
  const [latestResult, healthResult, predictionResult, alarmResult] = await Promise.allSettled([
    api.latest(id),
    api.healthDevices(),
    api.prediction(id),
    api.alarms({ deviceId: id, pageSize: 6 }),
  ])
  latest.value = latestResult.status === 'fulfilled' ? latestResult.value : null
  if (healthResult.status === 'fulfilled') {
    health.value = healthResult.value.devices.find((row) => row.device_id === id) || null
    healthComputed.value = healthResult.value.computed_at
  } else {
    health.value = null
    healthComputed.value = ''
  }
  prediction.value = predictionResult.status === 'fulfilled' ? predictionResult.value : null
  alarms.value = alarmResult.status === 'fulfilled' ? alarmResult.value.list : []
  loading.value = false
  if (!startText.value) applyPreset(1)
  else void loadChart()
}

const refresh = () => {
  void loadAll()
}

/* 实时遥测：只替换同设备的最新采样，历史由查询按钮决定 */
let timer: number | undefined
onMounted(() => {
  void loadAll()
  timer = window.setInterval(() => {
    if (shell.pulse && shell.pulse.device_id === deviceID.value) latest.value = shell.pulse
  }, 2000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})

watch(() => route.params.id, () => {
  device.value = null
  latest.value = null
  health.value = null
  prediction.value = null
  points.value = []
  startText.value = ''
  void loadAll()
})

/* ---------------- 展示辅助 ---------------- */

const scoreTone = computed(() => {
  const value = health.value?.score
  if (value == null) return 'idle'
  if (value >= 88) return 'ok'
  if (value >= 72) return 'warn'
  return 'bad'
})

/** 状态取真后端 health.go 的 state（online / offline / fault / unknown），不再从 status 反推 */
const stateLabel = computed(() => {
  const state = health.value?.state
  return state ? HEALTH_STATE_CN[state] || state : '未知'
})

const stateTone = computed(() => toneOf(health.value?.state || 'unknown'))

const riskTone = computed(() => (prediction.value?.risk_level === 'high' ? 'bad' : prediction.value ? 'ok' : 'idle'))
const riskLabel = computed(() => (prediction.value?.risk_level === 'high' ? '高' : prediction.value ? '低' : '无'))

const metrics = computed(() => {
  const row = latest.value
  return [
    { key: 'temperature', label: '温度', value: row?.temperature, unit: '℃' },
    { key: 'power', label: '功率', value: row?.power, unit: 'kW' },
    { key: 'voltage', label: '电压', value: row?.voltage, unit: 'V' },
    { key: 'current', label: '电流', value: row?.current, unit: 'A' },
  ]
})

const metricMeta = computed(() => ({
  unit: METRIC_UNIT[metric.value] || '',
  digits: METRIC_DIGITS[metric.value] ?? 1,
  label: METRIC_CN[metric.value] || metric.value,
}))

const reading = (alarm: Alarm) =>
  `${n(alarm.value, METRIC_DIGITS[alarm.metric] ?? 1)}${METRIC_UNIT[alarm.metric] || ''}`

const factorText = (feature: string, value: number) =>
  `${n(value, Math.abs(value) < 1 ? 2 : 1)}${FEATURE_UNIT[feature] || ''}`

const alarmTone = (alarm: Alarm) => toneOf(alarm.level)
</script>

<template>
  <div class="back-line">
    <button class="btn btn-sm" @click="router.push('/devices')">
      <Icon name="chevronLeft" :size="14" />返回设备列表
    </button>
  </div>

  <div v-if="failed" class="error-box">{{ failed }}</div>

  <div v-if="loading" class="card loading-card">读取设备详情…</div>

  <template v-else-if="device">
    <div class="page-head">
      <h1>{{ device.name }}</h1>
      <div class="page-actions">
        <span class="chip is-plain">设备 ID {{ device.id }}</span>
        <span class="chip" :class="device.is_simulated ? 'is-brand' : 'is-ok'">
          <i></i>{{ device.is_simulated ? '仿真设备' : '真实设备' }}
        </span>
        <button class="btn" :disabled="chartLoading" @click="refresh">
          <Icon name="refresh" :size="15" />刷新
        </button>
      </div>
    </div>

    <div class="detail-grid">
      <section class="card">
        <div class="card-head"><span class="card-title">设备档案</span></div>
        <div class="card-body">
          <dl class="kv">
            <dt>设备编号</dt>
            <dd class="num">{{ device.device_code }}</dd>
            <dt>设备类型</dt>
            <dd>{{ device.dev_type }}</dd>
            <dt>所属场站</dt>
            <dd>{{ device.station_code }}</dd>
            <dt>厂商</dt>
            <dd>{{ device.vendor || '未填写' }}</dd>
            <dt>分组</dt>
            <dd>{{ device.group_name || '未分组' }}</dd>
            <template v-if="device.is_simulated">
              <dt>设备性质</dt>
              <dd>仿真设备，遥测并非真机数据</dd>
              <dt>仿真参考型号</dt>
              <dd>{{ device.reference_vendor }} {{ device.reference_model }}</dd>
            </template>
            <dt>建档时间</dt>
            <dd class="num">{{ stamp(device.created_at) }}</dd>
          </dl>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">健康评分</span>
          <span class="card-sub">{{ health?.rule_version || 'rule' }}</span>
        </div>
        <div class="card-body">
          <div class="score-row">
            <div class="score-big" :class="`t-${scoreTone}`">
              <strong class="num">{{ health?.score == null ? '未知' : n(health.score, 0) }}</strong>
              <small>/ 100</small>
            </div>
            <div class="score-tags">
              <StatusChip :tone="stateTone" :label="stateLabel" />
              <span class="chip is-plain">排名 {{ health?.rank ?? '—' }}</span>
              <span v-if="health?.active_level" class="chip is-warn"><i></i>{{ LEVEL_CN[health.active_level] || health.active_level }}</span>
            </div>
          </div>
          <dl class="kv compact">
            <dt>评分依据</dt>
            <dd>{{ healthBasis(health?.matched_by) }}</dd>
            <dt>计算时间</dt>
            <dd class="num">{{ stamp(healthComputed) }}</dd>
            <dt>采样时间</dt>
            <dd class="num">{{ health?.sampled_at_ms ? stamp(new Date(health.sampled_at_ms).toISOString()) : '—' }}</dd>
            <dt>接收时间</dt>
            <dd class="num">{{ stamp(health?.received_at) }}</dd>
          </dl>
          <p class="card-foot">
            分数只由有效遥测参与计算；离线不代表硬件故障，未知状态不参与排名，也不拉低场站均分。
          </p>
        </div>
      </section>

      <section class="card latest-card">
        <div class="card-head">
          <span class="card-title">最新遥测</span>
          <span class="card-sub">{{ latest ? `${across(latest.received_at)}接收` : '暂无数据' }}</span>
          <span v-if="latest" class="card-sub right">
            运行状态 {{ TELEMETRY_STATE_CN[latest.status] ?? '未知' }} · 故障码 {{ latest.fault_code }}
          </span>
        </div>
        <div class="card-body">
          <div v-if="latest" class="tele-grid">
            <div v-for="item in metrics" :key="item.key" class="tele-cell">
              <span class="tele-label">{{ item.label }}</span>
              <span class="tele-value num">
                {{ item.value == null ? '—' : n(item.value, 1) }}<em>{{ item.unit }}</em>
              </span>
            </div>
          </div>
          <p v-else class="empty">设备尚无最新遥测，检查模拟器与数据链路。</p>
        </div>
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <span class="card-title">历史遥测曲线</span>
        <span class="card-sub">{{ metricMeta.label }}（{{ metricMeta.unit || '—' }}）· {{ points.length }} 个绘图点</span>
        <div class="card-tools">
          <select v-model="metric" class="select" aria-label="选择指标" @change="loadChart">
            <option value="temperature">温度 (℃)</option>
            <option value="power">功率 (kW)</option>
            <option value="voltage">电压 (V)</option>
            <option value="current">电流 (A)</option>
          </select>
          <div class="seg">
            <button v-for="hours in [0.25, 1, 4, 24]" :key="hours" :class="{ 'is-on': preset === hours }" @click="applyPreset(hours)">
              {{ hours < 1 ? '15 分钟' : `${hours} 小时` }}
            </button>
          </div>
        </div>
      </div>
      <div class="range-row">
        <label class="range-field">
          <span>开始</span>
          <input v-model="startText" type="datetime-local" class="field" />
        </label>
        <span class="range-dash">→</span>
        <label class="range-field">
          <span>结束</span>
          <input v-model="endText" type="datetime-local" class="field" />
        </label>
        <button class="btn btn-primary" :disabled="chartLoading" @click="loadChart">
          <Icon name="search" :size="14" />{{ chartLoading ? '查询中…' : '查询' }}
        </button>
        <button class="btn" :disabled="exporting" @click="exportCsv">
          <Icon name="download" :size="14" />{{ exporting ? '导出中…' : '导出 CSV' }}
        </button>
        <span class="range-note">单次 ≤ 24 小时，超过 6 小时自动分段取数</span>
      </div>

      <div v-if="chartError" class="error-box inset">{{ chartError }}</div>
      <div v-if="exportError" class="error-box inset">{{ exportError }}</div>

      <div class="chart-wrap">
        <PowerChart :points="points" :unit="metricMeta.unit" :name="metricMeta.label" :height="260" />
      </div>
      <div class="card-foot chart-foot">
        <span class="legend"><i></i>{{ metricMeta.label }}</span>
        <span class="foot-note">
          查询窗口 {{ startText.replace('T', ' ') }} — {{ endText.replace('T', ' ') }}（北京时间）
        </span>
      </div>
    </section>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <span class="card-title">该设备告警</span>
          <div class="card-tools">
            <RouterLink class="btn btn-sm" :to="`/alarms?q=${encodeURIComponent(device.device_code)}`">全部告警</RouterLink>
          </div>
        </div>
        <div v-if="alarms.length" class="alarm-list">
          <div v-for="alarm in alarms" :key="alarm.id" class="alarm-row">
            <StatusChip :tone="alarmTone(alarm)" :label="LEVEL_CN[alarm.level] || alarm.level" />
            <span class="alarm-metric">{{ METRIC_CN[alarm.metric] || alarm.metric }}</span>
            <span class="alarm-value num">{{ reading(alarm) }}<em>/ {{ n(alarm.threshold, METRIC_DIGITS[alarm.metric] ?? 1) }}{{ METRIC_UNIT[alarm.metric] }}</em></span>
            <span class="alarm-time num">{{ across(alarm.triggered_at) }}</span>
            <span class="alarm-state">{{ alarm.status === 'unhandled' ? '未处理' : alarm.status === 'acked' ? '已确认' : '已恢复' }}</span>
          </div>
        </div>
        <p v-else class="empty">该设备当前没有未归档告警。</p>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">失效风险</span>
          <span class="card-sub">{{ prediction?.model_version || '暂无模型结果' }}</span>
          <span class="card-sub right">{{ prediction?.stale ? '结果已过期' : '' }}</span>
        </div>
        <div class="card-body">
          <template v-if="prediction">
            <div class="risk-head">
              <span class="risk-prob num">{{ pct((prediction.probability ?? 0) * 100, 1) }}</span>
              <StatusChip :tone="riskTone" :label="`风险${riskLabel}`" />
              <span class="risk-threshold num">阈值 {{ pct((prediction.threshold ?? 0) * 100, 0) }}</span>
            </div>
            <div class="risk-track">
              <i :style="{ width: `${Math.min(100, (prediction.probability ?? 0) * 100)}%` }"></i>
              <span class="thr" :style="{ left: `${Math.min(100, (prediction.threshold ?? 0) * 100)}%` }"></span>
            </div>
            <div v-if="prediction.top_factors?.length" class="factors">
              <div v-for="factor in prediction.top_factors" :key="factor.feature" class="factor">
                <span class="factor-name">{{ FEATURE_CN[factor.feature] || factor.feature }}</span>
                <span class="factor-track"><i :style="{ width: `${Math.min(100, factor.contribution * 100)}%` }"></i></span>
                <span class="factor-value num">{{ factorText(factor.feature, factor.value) }}</span>
              </div>
            </div>
          </template>
          <p v-else class="empty">该设备尚无预测结果。</p>
        </div>
      </section>
    </div>
  </template>
</template>

<style scoped>
.back-line {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}


.loading-card {
  padding: 40px;
  text-align: center;
  color: var(--ink-3);
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}

.latest-card {
  grid-column: span 2;
}

.card-sub.right {
  margin-left: auto;
}

.score-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding-bottom: 14px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--line-soft);
}

.score-big {
  display: flex;
  align-items: baseline;
  gap: 6px;
  color: var(--ink);
}

.score-big strong {
  font-size: 38px;
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1;
}

.score-big small {
  font-size: 12px;
  color: var(--ink-3);
}

.score-big.t-ok strong {
  color: var(--ok-deep);
}

.score-big.t-warn strong {
  color: var(--warn-deep);
}

.score-big.t-bad strong {
  color: var(--bad-deep);
}

.score-tags {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.kv.compact {
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 7px 12px;
  font-size: 12px;
}

.tele-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.tele-cell {
  padding: 14px 16px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.tele-label {
  font-size: 11.5px;
  color: var(--ink-3);
}

.tele-value {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
}

.tele-value em {
  font-style: normal;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--ink-3);
  margin-left: 4px;
}

.range-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 0 20px 14px;
}

.range-field {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.range-field .field {
  min-width: 168px;
  height: 32px;
}

.range-dash {
  color: var(--ink-4);
}

.range-note {
  font-size: 11px;
  color: var(--ink-4);
}

.chart-wrap {
  padding: 0 14px 0 4px;
}

.chart-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.legend {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.legend i {
  width: 14px;
  height: 3px;
  border-radius: 2px;
  background: var(--brand);
}

.foot-note {
  margin-left: auto;
  color: var(--ink-4);
}

.error-box.inset {
  margin: 0 20px 12px;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}

.alarm-list {
  display: flex;
  flex-direction: column;
  padding: 0 20px 16px;
}

.alarm-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 0;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12px;
}

.alarm-row:last-child {
  border-bottom: 0;
}

.alarm-metric {
  color: var(--ink-2);
  min-width: 42px;
}

.alarm-value {
  color: var(--ink);
  font-weight: 600;
}

.alarm-value em {
  font-style: normal;
  font-weight: 500;
  color: var(--ink-4);
  margin-left: 4px;
}

.alarm-time {
  color: var(--ink-4);
  margin-left: auto;
}

.alarm-state {
  color: var(--ink-3);
  min-width: 42px;
  text-align: right;
}

.risk-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.risk-prob {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
}

.risk-threshold {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--ink-4);
}

.risk-track {
  position: relative;
  height: 8px;
  border-radius: var(--r-pill);
  background: #e9eef7;
}

.risk-track i {
  display: block;
  height: 100%;
  border-radius: var(--r-pill);
  background: var(--brand);
}

.risk-track .thr {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 2px;
  border-radius: 2px;
  background: var(--ink-2);
}

.factors {
  display: flex;
  flex-direction: column;
  gap: 9px;
  margin-top: 16px;
}

.factor {
  display: grid;
  grid-template-columns: 132px minmax(0, 1fr) 54px;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--ink-2);
}

.factor-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.factor-track {
  height: 6px;
  border-radius: var(--r-pill);
  background: #e9eef7;
  overflow: hidden;
}

.factor-track i {
  display: block;
  height: 100%;
  border-radius: var(--r-pill);
  background: var(--brand);
}

.factor-value {
  text-align: right;
  color: var(--ink-3);
}

@media (max-width: 1180px) {
  .detail-grid,
  .two-col {
    grid-template-columns: minmax(0, 1fr);
  }

  .latest-card {
    grid-column: span 1;
  }
}

@media (max-width: 900px) {
  .tele-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .range-field .field {
    min-width: 0;
    width: 100%;
  }

  .range-field {
    flex: 1 1 100%;
  }
}

@media (max-width: 680px) {
  .tele-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .score-row {
    flex-wrap: wrap;
  }
}
</style>
