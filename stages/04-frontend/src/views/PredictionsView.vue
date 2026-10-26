<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { contributionNumber, fixedNumber, predictionFeature } from '../lib/detailPresentation'
import { realtime } from '../lib/realtime'
import { dateTime, errorMessage, predictionState } from '../lib/format'
import type { Prediction, PredictionListItem } from '../types'

const router = useRouter()
const rows = ref<PredictionListItem[]>([])
const detail = ref<Prediction | null>(null)
const selectedDevice = ref<PredictionListItem | null>(null)
const modelVersion = computed(() => detail.value?.model_version || selectedDevice.value?.model_version || '')
const shortModelVersion = computed(() => {
  const version = modelVersion.value
  if (!version) return '—'
  const xgb = /^stage\d+-xgb-([a-f0-9]+)$/i.exec(version)
  if (xgb) return `XGB · ${xgb[1].slice(0, 6)}`
  return version.length > 20 ? `${version.slice(0, 12)}…${version.slice(-6)}` : version
})
const drawer = ref(false)
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const error = ref('')
const detailError = ref('')
let unsubscribe: (() => void) | null = null
let periodic: ReturnType<typeof setInterval> | null = null

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try { const result = await api.predictions(page.value, 20); rows.value = result.list || []; total.value = result.total }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function openPrediction(row: PredictionListItem): Promise<void> {
  selectedDevice.value = row
  detail.value = null
  detailError.value = ''
  drawer.value = true
  if (row.probability === null) return
  try { detail.value = await api.prediction(row.device_id) }
  catch (cause) { detailError.value = errorMessage(cause) }
}

function probability(value: number | null): string { return value === null ? '—' : `${(value * 100).toFixed(1)}%` }

onMounted(() => {
  void load()
  unsubscribe = realtime.subscribe((event) => {
    if (event.type === 'connected' || event.type === 'prediction') {
      void load()
      if (event.type === 'prediction' && selectedDevice.value?.device_id === event.data.device_id) void openPrediction(selectedDevice.value)
    }
  })
  periodic = setInterval(() => { void load() }, 60000)
})
onUnmounted(() => { unsubscribe?.(); if (periodic) clearInterval(periodic) })
</script>

<template>
  <div class="page-narrow">
  <section class="page-head"><div><h1>故障预测</h1><p>按设备查看未来一小时故障风险和模型给出的主要特征贡献。</p></div><div class="head-stat">设备总数 <strong>{{ total }}</strong></div></section>
  <el-alert title="当前模型使用合成数据训练；概率与特征贡献仅用于演示，不能代表真实场站预测效果。" type="warning" show-icon :closable="false" class="content-alert" />
  <section class="surface"><div class="toolbar"><div class="toolbar-title">设备风险排行 <span>有效预测优先按故障概率排序；过期和无结果排在后面</span></div><el-button @click="load">刷新结果</el-button></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
    <el-table v-loading="loading" :data="rows" stripe empty-text="暂无设备" class="data-table" @row-click="(row: PredictionListItem) => openPrediction(row)"><el-table-column label="设备" min-width="190"><template #default="{ row }"><strong>{{ row.name }}</strong><small class="cell-sub">{{ row.device_code }}</small></template></el-table-column><el-table-column label="所属场站" prop="station_code" min-width="120" /><el-table-column label="故障概率" min-width="170"><template #default="{ row }"><div class="risk-cell"><strong :class="{ 'risk-high': row.risk_level === 'high' && !row.stale }">{{ probability(row.probability) }}</strong><el-progress v-if="row.probability !== null" :percentage="Math.round(row.probability * 100)" :stroke-width="5" :show-text="false" :color="row.stale ? '#c9cdd4' : row.risk_level === 'high' ? '#f53f3f' : '#006eff'" /></div></template></el-table-column><el-table-column label="状态" min-width="130"><template #default="{ row }"><el-tag v-if="predictionState(row) === 'fresh'" :type="row.risk_level === 'high' ? 'danger' : 'success'" effect="plain">{{ row.risk_level === 'high' ? '高风险' : '低风险' }}</el-tag><el-tag v-else-if="predictionState(row) === 'stale'" type="info" effect="plain">结果过期</el-tag><el-tag v-else type="info" effect="plain">暂无预测</el-tag></template></el-table-column><el-table-column label="窗口时间" min-width="175"><template #default="{ row }">{{ dateTime(row.window_end_ms) }}</template></el-table-column><el-table-column label="操作" width="115"><template #default="{ row }"><el-button link type="primary" @click.stop="openPrediction(row)">查看依据 →</el-button></template></el-table-column></el-table>
    <div class="table-footer"><span>共 {{ total }} 台设备</span><el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" /></div>
  </section>
  </div>
  <el-drawer v-model="drawer" title="预测依据" size="min(560px, 100vw)">
    <div v-if="selectedDevice" class="drawer-content prediction-detail">
      <section class="prediction-overview" aria-label="设备与故障风险">
        <p class="prediction-device-code">{{ selectedDevice.device_code }}</p>
        <h2>{{ selectedDevice.name }}</h2>
        <div v-if="selectedDevice.probability !== null" class="prediction-risk" :class="{ 'is-high': selectedDevice.risk_level === 'high' && !selectedDevice.stale, 'is-stale': selectedDevice.stale || detail?.stale }">
          <span>未来一小时故障概率</span>
          <strong>{{ probability(selectedDevice.probability) }}</strong>
        </div>
      </section>
      <div v-if="selectedDevice.probability === null" class="empty-box">该设备尚无预测结果。需要有完整且新鲜的遥测窗口。</div>
      <template v-else>
        <el-alert v-if="selectedDevice.stale || detail?.stale" title="预测已超过 15 分钟，不作为当前风险判断依据。" type="warning" :closable="false" show-icon />
        <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
        <dl class="prediction-meta-grid" aria-label="预测信息">
          <div class="prediction-meta-item"><dt>预测窗口</dt><dd>{{ dateTime(detail?.window_end_ms ?? selectedDevice.window_end_ms) }}</dd></div>
          <div class="prediction-meta-item"><dt>风险阈值</dt><dd>{{ probability(detail?.threshold ?? selectedDevice.threshold) }}</dd></div>
          <div class="prediction-meta-item"><dt>模型版本</dt><dd :title="modelVersion || undefined">{{ shortModelVersion }}</dd></div>
          <div class="prediction-meta-item"><dt>模型来源</dt><dd>{{ detail?.source === 'synthetic-trained' || selectedDevice.source === 'synthetic-trained' ? '合成数据训练' : (detail?.source || selectedDevice.source || '—') }}</dd></div>
        </dl>
        <h3>主要特征贡献</h3>
        <table v-if="detail?.top_factors?.length" class="factor-table" aria-label="主要特征贡献"><colgroup><col /><col class="factor-value-column" /><col class="factor-unit-column" /><col class="factor-contribution-column" /></colgroup><thead><tr><th scope="col">影响因素</th><th scope="col">特征值</th><th scope="col" class="factor-unit-heading">单位</th><th scope="col">模型贡献</th></tr></thead><tbody><tr v-for="(factor, index) in detail.top_factors" :key="factor.feature"><th scope="row"><span class="factor-rank">{{ String(index + 1).padStart(2, '0') }}</span><span :title="predictionFeature(factor.feature).description">{{ predictionFeature(factor.feature).label }}</span></th><td class="factor-value">{{ fixedNumber(factor.value) }}</td><td class="factor-unit">{{ fixedNumber(factor.value) !== '—' ? predictionFeature(factor.feature).unit : '' }}</td><td class="factor-contribution" :class="{ 'is-positive': factor.contribution > 0, 'is-negative': factor.contribution < 0 }">{{ contributionNumber(factor.contribution) }}</td></tr></tbody></table>
        <div v-else class="empty-box">暂无可展示的特征贡献。</div>
        <p class="drawer-note">贡献为正表示推高模型风险评分，为负表示拉低评分；不是概率，也不等同于实际故障原因。</p>
      </template>
      <el-button class="drawer-link" @click="router.push(`/devices/${selectedDevice?.device_id}`)">查看设备历史曲线 →</el-button>
    </div>
  </el-drawer>
</template>
