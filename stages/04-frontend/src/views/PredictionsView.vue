<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import Drawer from '../components/Drawer.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import type { Prediction, PredictionRow, StationRow } from '../lib/api'
import { FEATURE_CN, FEATURE_UNIT } from '../lib/data'
import { n, pct, stamp, timeOf } from '../lib/format'

const route = useRoute()
const router = useRouter()

/** 概率来源：真后端给 scheduled，历史 mock 给 synthetic-trained */
const sourceLabel = (source: string | null) => {
  if (!source) return '—'
  if (source === 'scheduled') return '定时计算'
  if (source === 'synthetic-trained') return '合成数据训练'
  return source
}

/** 概率与阈值的差，单位 pt（百分点）；负数表示低于阈值 */
const thresholdGap = (probability: number | null, threshold: number | null) =>
  probability == null || threshold == null ? null : (probability - threshold) * 100

/** 模板里不能写 TS 非空断言，差值文案在脚本里拼好 */
const gapText = (probability: number | null, threshold: number | null) => {
  const gap = thresholdGap(probability, threshold)
  if (gap == null) return ''
  return `${gap >= 0 ? '+' : '−'}${n(Math.abs(gap), 1)} pt`
}

/** 表格里的概率条：概率缺失时按 0 画，文字显示「—」，不把 null 当 0 展示 */
const fillOf = (row: { probability: number | null }) => Math.min(100, Math.max(0, (row.probability ?? 0) * 100))
const markOf = (row: { threshold: number | null }) => Math.max(0, Math.min(100, (row.threshold ?? 0) * 100))
const probText = (row: { probability: number | null }) =>
  row.probability == null ? '—' : pct(row.probability * 100, 1)
const isHighOf = (row: { probability: number | null; threshold: number | null }) =>
  row.probability != null && row.threshold != null && row.probability >= row.threshold

const rows = ref<PredictionRow[]>([])
const stations = ref<StationRow[]>([])
const loading = ref(true)
const failed = ref('')

type RiskKey = 'all' | 'high' | 'low'
const riskOptions: { key: RiskKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'high', label: '高风险' },
  { key: 'low', label: '低风险' },
]
const riskFilter = ref<RiskKey>('all')
const keyword = ref('')
const page = ref(1)
const pageSize = 8
const selected = ref<PredictionRow | null>(null)

const load = async () => {
  loading.value = true
  try {
    const [predictionPayload, stationPayload] = await Promise.all([
      api.predictions(1, 100),
      api.stations(),
    ])
    rows.value = predictionPayload.list
    stations.value = stationPayload.list
    failed.value = ''
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}
onMounted(load)

const applyQuery = () => {
  const risk = route.query.risk as string | undefined
  if (risk === 'high' || risk === 'low') riskFilter.value = risk
  const q = route.query.q as string | undefined
  if (q) keyword.value = q
}
applyQuery()
watch(
  () => route.query,
  () => {
    applyQuery()
    page.value = 1
  },
)
watch([riskFilter, keyword], () => {
  page.value = 1
  const query: Record<string, string> = {}
  if (riskFilter.value !== 'all') query.risk = riskFilter.value
  if (keyword.value.trim()) query.q = keyword.value.trim()
  router.replace({ query })
})

const stationName = computed(() => {
  const map = new Map<string, string>()
  for (const item of stations.value) map.set(item.station_code, item.station_name)
  return map
})

const filtered = computed(() => {
  const key = keyword.value.trim().toUpperCase()
  return rows.value
    .filter((row) => {
      if (riskFilter.value !== 'all' && row.risk_level !== riskFilter.value) return false
      if (!key) return true
      return (
        row.device_code.toUpperCase().includes(key) ||
        (row.name || '').toUpperCase().includes(key) ||
        row.station_code.includes(key)
      )
    })
    .sort((a, b) => (b.probability ?? -1) - (a.probability ?? -1))
})

const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
watch(totalPages, (value) => {
  if (page.value > value) page.value = value
})

const highCount = computed(() => rows.value.filter((row) => row.risk_level === 'high').length)
const topProbability = computed(() =>
  rows.value.length ? Math.max(...rows.value.map((row) => row.probability ?? 0)) : null,
)
const averageProbability = computed(() =>
  rows.value.length
    ? rows.value.reduce((sum, row) => sum + (row.probability ?? 0), 0) / rows.value.length
    : null,
)
const modelVersion = computed(() => rows.value[0]?.model_version || '—')
const threshold = computed(() => rows.value[0]?.threshold ?? 0.62)

const strip = computed(() => [
  { key: 'high', label: '高风险', color: 'var(--bad)', value: `${highCount.value}`, note: `概率 ≥ ${pct(threshold.value * 100, 0)}` },
  { key: 'low', label: '低风险', color: 'var(--ok)', value: `${rows.value.length - highCount.value}`, note: '未达阈值' },
  { key: 'top', label: '最高概率', color: 'var(--warn)', value: topProbability.value == null ? '—' : pct(topProbability.value * 100, 1), note: '取全部预测设备' },
  { key: 'avg', label: '平均概率', color: 'var(--brand)', value: averageProbability.value == null ? '—' : pct(averageProbability.value * 100, 1), note: `${rows.value.length} 台有预测` },
])

const buckets = computed(() => {
  const edges = [0, 0.2, 0.4, 0.6, 0.8, 1.0001]
  const labels = ['0–20%', '20–40%', '40–60%', '60–80%', '80–100%']
  const counts = labels.map((label, index) => {
    const count = rows.value.filter(
      (row) => (row.probability ?? -1) >= edges[index] && (row.probability ?? -1) < edges[index + 1],
    ).length
    return { label, count, high: edges[index] >= threshold.value }
  })
  const max = Math.max(1, ...counts.map((item) => item.count))
  return counts.map((item) => ({ ...item, width: (item.count / max) * 100 }))
})

const isStale = computed(() => rows.value.some((row) => row.stale))

const detail = ref<Prediction | null>(null)

/** 列表接口只给概率与阈值；特征贡献在详情接口里，打开抽屉时补一次 */
const loadDetail = async (deviceId: number) => {
  detail.value = null
  try {
    detail.value = await api.prediction(deviceId)
  } catch {
    /* 详情失败就只显示列表已有信息，不影响抽屉打开 */
  }
}

const openRow = (row: PredictionRow) => {
  selected.value = row
  router.replace({ query: { ...route.query, id: String(row.device_id) } })
  void loadDetail(row.device_id)
}
const closeDrawer = () => {
  selected.value = null
  detail.value = null
  const query = { ...route.query }
  delete query.id
  router.replace({ query })
}
watch(
  () => [rows.value, route.query.id] as const,
  () => {
    const id = Number(route.query.id)
    if (!id) return
    const hit = rows.value.find((row) => row.device_id === id)
    if (hit) {
      selected.value = hit
      void loadDetail(hit.device_id)
    }
  },
)
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" :disabled="loading" @click="load">
        <Icon name="refresh" :size="15" />刷新
      </button>
    </div>
  </div>

  <div v-if="failed" class="error-box">预测数据未取到：{{ failed }}</div>

  <section class="card strip">
    <div v-for="item in strip" :key="item.key" class="strip-cell">
      <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
      <span class="strip-value num">{{ item.value }}</span>
      <span class="strip-note">{{ item.note }}</span>
    </div>
  </section>

  <div class="pd-grid">
    <section class="card">
      <div class="card-head">
        <span class="card-title">预测清单</span>
        <span class="card-sub">{{ filtered.length }} 台匹配 · 按概率降序</span>
        <div class="card-tools">
          <label class="field-search">
            <Icon name="search" :size="14" />
            <input v-model="keyword" type="text" placeholder="编码 / 名称 / 场站" />
          </label>
        </div>
      </div>

      <div class="filters">
        <div class="seg">
          <button
            v-for="item in riskOptions"
            :key="item.key"
            :class="{ 'is-on': riskFilter === item.key }"
            @click="riskFilter = item.key"
          >
            {{ item.label }}
          </button>
        </div>
      </div>

      <div class="table-wrap">
        <table class="table pd-table">
          <colgroup>
            <col style="width: 34%" />
            <col style="width: 16%" />
            <col style="width: 26%" />
            <col style="width: 11%" />
            <col style="width: 13%" />
          </colgroup>
          <thead>
            <tr>
              <th>设备</th>
              <th>场站</th>
              <th>失效概率</th>
              <th>等级</th>
              <th>窗口结束</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in paged" :key="row.device_id" class="row-link" @click="openRow(row)">
              <td>
                <div class="cell-main">
                  <span class="cell-icon"><Icon name="cpu" :size="15" /></span>
                  <div class="cell-stack">
                    <span class="cell-name">{{ row.name || row.device_code }}</span>
                    <span class="cell-note">{{ row.device_code }}</span>
                  </div>
                </div>
              </td>
              <td class="station">
                <span class="station-code">{{ row.station_code }}</span>
                <span class="cell-note">{{ stationName.get(row.station_code) || '' }}</span>
              </td>
              <td>
                <div class="prob">
                  <span class="prob-track">
                    <i
                      class="prob-fill"
                      :class="{ 'is-high': isHighOf(row) }"
                      :style="{ width: `${fillOf(row)}%` }"
                    ></i>
                    <em class="prob-mark" :style="{ left: `${markOf(row)}%` }"></em>
                  </span>
                  <b class="num prob-value">{{ probText(row) }}</b>
                </div>
              </td>
              <td>
                <StatusChip :tone="row.risk_level === 'high' ? 'bad' : 'ok'" :label="row.risk_level === 'high' ? '高风险' : '低风险'" />
              </td>
              <td class="num window">{{ timeOf(row.window_end_ms) }}</td>
            </tr>
            <tr v-if="!paged.length && !loading">
              <td colspan="5" class="empty">没有符合条件的预测</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <button class="pager-btn" :disabled="page <= 1" title="上一页" @click="page -= 1">
          <Icon name="chevronLeft" :size="15" />
        </button>
        <span class="pager-page num">{{ page }}</span>
        <span class="pager-total">/ {{ totalPages }}</span>
        <button class="pager-btn" :disabled="page >= totalPages" title="下一页" @click="page += 1">
          <Icon name="chevronRight" :size="15" />
        </button>
        <span class="grow"></span>
        <span class="pager-total">竖线为阈值 {{ pct(threshold * 100, 0) }}</span>
      </div>
    </section>

    <aside class="pd-side">
      <section class="card">
        <div class="card-head">
          <span class="card-title">概率分布</span>
          <span class="card-sub">共 {{ rows.length }} 台</span>
        </div>
        <div class="buckets">
          <div v-for="item in buckets" :key="item.label" class="bucket">
            <span class="bucket-label num">{{ item.label }}</span>
            <span class="bucket-track">
              <i :class="{ 'is-high': item.high }" :style="{ width: `${item.width}%` }"></i>
            </span>
            <b class="num bucket-count">{{ item.count }}</b>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">模型与口径</span>
        </div>
        <div class="note-body">
          <p>模型版本 <b class="num">{{ modelVersion }}</b>，阈值 <b class="num">{{ pct(threshold * 100, 0) }}</b>。</p>
          <p>只对抽样设备给出预测；无预测不等于无风险，可能是该设备尚未进入计算窗口。</p>
          <p v-if="isStale" class="warn-line">存在过期预测，结果可能滞后于最新遥测。</p>
          <p v-else>本轮预测均基于最新窗口。</p>
        </div>
      </section>
    </aside>
  </div>

  <Drawer
    v-if="selected"
    :title="selected.name || selected.device_code"
    :sub="`${selected.device_code} · ${stationName.get(selected.station_code) || selected.station_code}${selected.group_name ? ' · ' + selected.group_name : ''}`"
    @close="closeDrawer"
  >
    <!-- 概率是这张抽屉唯一要回答的问题，占最大的一格 -->
    <section
      class="pred-hero"
      :class="{ 'is-high': selected.risk_level === 'high' && !selected.stale, 'is-stale': selected.stale }"
    >
      <div class="pred-hero-head">
        <span class="pred-hero-label">未来一小时故障概率</span>
        <span class="pred-risk" :class="selected.risk_level === 'high' ? 'is-bad' : 'is-ok'">
          {{ selected.risk_level === 'high' ? '高风险' : '低风险' }}
        </span>
      </div>
      <b class="pred-hero-value num">{{ selected.probability == null ? '—' : pct(selected.probability * 100, 1) }}</b>
      <p class="pred-hero-note">
        阈值 {{ selected.threshold == null ? '—' : pct(selected.threshold * 100, 0) }}
        <template v-if="gapText(selected.probability, selected.threshold)">
          · 距阈值 <b class="num">{{ gapText(selected.probability, selected.threshold) }}</b>
        </template>
      </p>
    </section>

    <div v-if="selected.stale" class="pred-alert">
      <Icon name="alert" :size="14" />
      <span>预测已超过 15 分钟，不作为当前风险判断依据。</span>
    </div>

    <section class="pred-meta">
      <div class="pred-meta-cell">
        <span>预测窗口</span>
        <b class="num">{{ selected.window_end_ms ? stamp(new Date(selected.window_end_ms).toISOString()) : '—' }}</b>
      </div>
      <div class="pred-meta-cell">
        <span>风险阈值</span>
        <b class="num">{{ selected.threshold == null ? '—' : pct(selected.threshold * 100, 0) }}</b>
      </div>
      <div class="pred-meta-cell">
        <span>模型版本</span>
        <b class="num">{{ selected.model_version || '—' }}</b>
      </div>
      <div class="pred-meta-cell">
        <span>模型来源</span>
        <b>{{ sourceLabel(selected.source) }}</b>
      </div>
    </section>

    <section class="pred-factors">
      <h3 class="pred-factors-title">主要特征贡献</h3>
      <table v-if="detail?.top_factors?.length" class="table pred-table">
        <colgroup>
          <col style="width: 46%" />
          <col style="width: 20%" />
          <col style="width: 14%" />
          <col style="width: 20%" />
        </colgroup>
        <thead>
          <tr>
            <th>影响因素</th>
            <th class="r">特征值</th>
            <th class="r">单位</th>
            <th class="r">模型贡献</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in (detail?.top_factors ?? [])" :key="item.feature">
            <td>
              <span class="factor-idx num">{{ String(index + 1).padStart(2, '0') }}</span>
              {{ FEATURE_CN[item.feature] || item.feature }}
            </td>
            <td class="r num">{{ n(item.value, 2) }}</td>
            <td class="r muted">{{ FEATURE_UNIT[item.feature] || '' }}</td>
            <td
              class="r num"
              :class="item.contribution > 0 ? 'is-up' : item.contribution < 0 ? 'is-down' : 'muted'"
            >
              {{ item.contribution > 0 ? '+' : '' }}{{ n(item.contribution, 3) }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">{{ detail ? '暂无可展示的特征贡献。' : '正在读取特征贡献…' }}</p>
    </section>

    <div class="panel-note">
      贡献为正表示推高模型风险评分，为负表示拉低评分；不是概率，也不等同于实际故障原因。
      概率来自固定窗口内的特征打分，阈值由规则版本控制。高风险只表示「建议优先核查」，
      不构成停机或更换建议；确认故障仍需现场检查与遥测复核。
    </div>

    <template #footer>
      <RouterLink v-if="selected.device_id" class="btn btn-primary" :to="`/devices/${selected.device_id}`">
        查看设备历史曲线<Icon name="arrowRight" :size="14" />
      </RouterLink>
      <RouterLink class="btn" :to="{ path: '/health', query: { q: selected.device_code } }">
        看健康分<Icon name="chevronRight" :size="14" />
      </RouterLink>
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

.pd-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 322px;
  gap: 16px;
  align-items: start;
}

.pd-side {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px 14px;
}

.pd-table {
  table-layout: fixed;
  min-width: 720px;
}

.station {
  line-height: 1.35;
}

.station-code {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink);
}

.prob {
  display: flex;
  align-items: center;
  gap: 10px;
}

.prob-track {
  position: relative;
  flex: 1;
  min-width: 0;
  max-width: 120px;
  height: 6px;
  border-radius: 4px;
  background: var(--line-soft);
  overflow: hidden;
}

.prob-fill {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  border-radius: 4px;
  background: var(--brand);
}

.prob-fill.is-high {
  background: var(--bad);
}

.prob-mark {
  position: absolute;
  top: -3px;
  bottom: -3px;
  width: 1.5px;
  background: var(--ink-3);
  opacity: 0.55;
}

.prob-value {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--ink);
  min-width: 44px;
  text-align: right;
}

.window {
  color: var(--ink-2);
  font-size: 12px;
}

.buckets {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 4px 20px 18px;
}

.bucket {
  display: flex;
  align-items: center;
  gap: 9px;
}

.bucket-label {
  font-size: 11px;
  color: var(--ink-3);
  width: 56px;
  flex: 0 0 56px;
}

.bucket-track {
  flex: 1;
  height: 7px;
  border-radius: 4px;
  background: var(--line-soft);
  overflow: hidden;
}

.bucket-track i {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--brand);
}

.bucket-track i.is-high {
  background: var(--bad);
}

.bucket-count {
  font-size: 11.5px;
  color: var(--ink-2);
  width: 16px;
  text-align: right;
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

.warn-line {
  color: var(--warn-deep);
}

/* 详情抽屉：概率是这张抽屉唯一的答案，占最大一格；元信息 2×2；特征贡献表 */
.pred-hero {
  padding: 15px 17px 13px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--card-soft);
}

.pred-hero.is-high {
  border-color: #f0c7c2;
  background: var(--bad-soft);
}

.pred-hero.is-stale {
  border-style: dashed;
  background: var(--card-soft);
}

.pred-hero-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.pred-hero-label {
  font-size: 11.5px;
  color: var(--ink-3);
}

.pred-risk {
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: #fff;
  white-space: nowrap;
}

.pred-risk.is-bad {
  color: var(--bad-deep);
  border-color: #f0c7c2;
}

.pred-risk.is-ok {
  color: var(--ok-deep);
  border-color: #c6e8d6;
}

.pred-hero-value {
  display: block;
  margin-top: 4px;
  font-size: 46px;
  line-height: 1.05;
  font-weight: 700;
  letter-spacing: -1.6px;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}

.pred-hero.is-high .pred-hero-value {
  color: var(--bad);
}

.pred-hero-note {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--ink-3);
}

.pred-hero-note b {
  color: var(--ink);
  font-weight: 600;
}

.pred-alert {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 9px 12px;
  border: 1px solid #f5dfb3;
  border-radius: var(--r-md);
  background: var(--warn-soft);
  color: var(--warn-deep);
  font-size: 11.5px;
}

.pred-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.pred-meta-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 11px 13px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--card-soft);
}

.pred-meta-cell span {
  font-size: 11px;
  color: var(--ink-3);
}

.pred-meta-cell b {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
  word-break: break-all;
}

.pred-factors {
  margin-top: 18px;
}

.pred-factors-title {
  margin: 0 0 8px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
}

.pred-table {
  table-layout: fixed;
  min-width: 0;
  width: 100%;
}

.factor-idx {
  display: inline-block;
  width: 22px;
  font-size: 10.5px;
  color: var(--ink-4);
}

.pred-table .is-up {
  color: var(--warn-deep);
  font-weight: 600;
}

.pred-table .is-down {
  color: var(--brand);
  font-weight: 600;
}

@media (max-width: 1240px) {
  .pd-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .pd-side {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
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

  .pd-side {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
