<script setup lang="ts">
import { computed } from 'vue'
import type { Interpretation } from '../types'
import { fixedNumber } from '../lib/detailPresentation'
import { metricLabels } from '../lib/format'
import { interpretationLayout } from '../lib/interpretation-layout'
import TelemetryChart from './TelemetryChart.vue'

const props = defineProps<{ item: Interpretation }>()
const emit = defineEmits<{ 'open-evidence': [ids: string[]] }>()
const display = computed(() => interpretationLayout(props.item))
const contribution = (value: number | null) => value == null ? '—' : `${value > 0 ? '+' : ''}${fixedNumber(value)}`
</script>

<template>
  <section class="reading-section" aria-label="模型解读要点">
    <div class="reading-heading"><h3>解读要点</h3><span>模型解读 · 触发时快照</span></div>
    <ol v-if="display.paragraphs.length" class="conclusion-points">
      <li v-for="(paragraph, index) in display.paragraphs" :key="index"><span class="point-number">{{ String(index + 1).padStart(2, '0') }}</span><p>{{ paragraph }}</p></li>
    </ol>
    <p v-else class="reading-empty">暂无可展示的结论，请核对事件证据。</p>
    <div v-if="item.result?.conclusion?.evidence_ids.length" class="evidence-chips"><span>结论依据</span><button v-for="id in item.result.conclusion.evidence_ids" :key="id" @click="emit('open-evidence', [id])">{{ id }}</button></div>
    <details class="original-conclusion"><summary>查看原始解读文本</summary><p>{{ item.result?.conclusion?.text }}</p></details>
  </section>

  <section v-if="display.history" class="history-summary" aria-label="近期同指标告警">
    <div><strong>近期同指标告警</strong><span>触发前 24 小时 · 已取回快照</span></div>
    <dl><div><dt>记录数</dt><dd>{{ display.history.count }} <small>条</small></dd></div><div><dt>记录值范围</dt><dd>{{ fixedNumber(display.history.min) }} <small>至</small> {{ fixedNumber(display.history.max) }}</dd></div></dl>
    <p v-if="!display.history.complete">历史列表存在截取；这里的条数与范围不代表全部历史。</p>
    <button class="evidence-link" @click="emit('open-evidence', [display.history!.id])">查看 {{ display.history.id }} 来源</button>
  </section>

  <section v-if="display.prediction" class="reading-section" aria-label="触发时模型因素">
    <div class="reading-heading"><h3>模型因素</h3><button class="evidence-link" @click="emit('open-evidence', [display.prediction!.id])">来源 {{ display.prediction.id }}</button></div>
    <table v-if="display.factors.length" class="interpretation-factors"><thead><tr><th scope="col">影响因素</th><th scope="col" class="feature-value-column">特征值</th><th scope="col" class="feature-unit-column">单位</th><th scope="col">模型贡献</th></tr></thead><tbody>
      <tr v-for="factor in display.factors" :key="factor.key"><th scope="row"><span :title="factor.description">{{ factor.label }}</span></th><td class="feature-value-column">{{ fixedNumber(factor.value) }}</td><td class="feature-unit-column">{{ factor.value != null && factor.unit ? factor.unit : '—' }}</td><td :class="{ positive: (factor.contribution ?? 0) > 0, negative: (factor.contribution ?? 0) < 0 }">{{ contribution(factor.contribution) }}</td></tr>
    </tbody></table>
    <p v-else class="reading-empty">此预测快照未提供可展示的特征贡献。</p>
    <p class="reading-note">正值推高模型风险评分，负值拉低评分；贡献不是概率，也不能认定实际故障原因。</p>
    <p v-if="display.modelVersion" class="model-version">模型版本 <span>{{ display.modelVersion }}</span></p>
  </section>

  <section v-if="display.telemetry && display.metric" class="reading-section" aria-label="触发前历史走势">
    <div class="reading-heading"><h3>{{ metricLabels[display.metric] }}走势</h3><button class="evidence-link" @click="emit('open-evidence', [display.telemetry!.id])">来源 {{ display.telemetry.id }}</button></div>
    <div class="telemetry-statistics"><div v-for="stat in display.statistics" :key="stat.label"><span>{{ stat.label }}</span><strong>{{ fixedNumber(stat.value) }} <small v-if="stat.value != null">{{ display.unit }}</small></strong></div></div>
    <TelemetryChart v-if="display.points.length" :points="display.points" :metric="display.metric" height="240px" />
    <p v-else class="reading-empty">当前证据未提供有效曲线点。</p>
    <p class="reading-note">统计基于 {{ display.sampleCount ?? '—' }} 个有效原始点；图中展示 {{ display.points.length }} 个实际采样点{{ display.telemetry.data.compressed ? '（已压缩）' : '' }}。这是触发时快照，非当前实时数据。</p>
  </section>
</template>

<style scoped>
.reading-section { margin-top: 22px; }
.reading-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
.reading-heading h3 { margin: 0; font-size: 14px; color: var(--ink); }
.reading-heading > span, .reading-note, .reading-empty, .model-version { color: var(--ink-3); font-size: 12px; line-height: 1.7; }
.conclusion-points { margin: 0; padding: 0; list-style: none; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--card-2); }
.conclusion-points li { display: grid; grid-template-columns: 28px minmax(0, 1fr); column-gap: 10px; padding: 14px 16px; }
.conclusion-points li + li { border-top: 1px solid var(--line); }
.point-number { font-size: 11px; color: var(--brand); line-height: 1.9; font-variant-numeric: tabular-nums; }
.conclusion-points p { margin: 0; font-size: 14px; line-height: 1.9; color: var(--ink-2); overflow-wrap: anywhere; }
.conclusion-points li:first-child p { color: var(--ink); font-weight: 500; }
.evidence-chips { display: flex; align-items: center; gap: 8px; margin: 12px 0; flex-wrap: wrap; font-size: 12px; color: var(--ink-3); }
button { cursor: pointer; font: inherit; }
.evidence-chips button { color: var(--brand); background: var(--brand-soft); border: 1px solid var(--line); border-radius: var(--r-sm); padding: 2px 9px; }
.evidence-link { color: var(--brand); background: transparent; border: 0; padding: 2px 0; font-size: 12px; }
.original-conclusion { font-size: 12px; color: var(--ink-3); }
.original-conclusion summary { cursor: pointer; }
.original-conclusion p { color: var(--ink-2); font-size: 13px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--card-2); border: 1px solid var(--line); }
.history-summary { margin-top: 18px; padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--r-md); }
.history-summary > div { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; }
.history-summary strong { font-size: 13px; color: var(--ink); }
.history-summary span, .history-summary p { font-size: 12px; color: var(--ink-3); }
.history-summary dl { display: flex; gap: 36px; flex-wrap: wrap; margin: 12px 0; }
.history-summary dt { color: var(--ink-3); font-size: 11px; margin-bottom: 5px; }
.history-summary dd { margin: 0; font-size: 17px; color: var(--ink); font-variant-numeric: tabular-nums; }
small { font-size: 11px; color: var(--ink-3); font-weight: 400; }
.interpretation-factors { width: 100%; border-collapse: collapse; font-size: 13px; }
.interpretation-factors thead th { color: var(--ink-3); font-weight: 400; font-size: 11px; background: var(--card-2); }
.interpretation-factors th, .interpretation-factors td { padding: 11px 12px; text-align: right; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
.interpretation-factors th:first-child { text-align: left; font-weight: 500; }
.interpretation-factors .feature-value-column { text-align: center; width: 20%; }
.interpretation-factors .feature-unit-column { text-align: center; width: 16%; color: var(--ink-3); font-size: 12px; font-weight: 400; }
.interpretation-factors td { color: var(--ink); white-space: nowrap; }
.interpretation-factors td.positive { color: var(--warning-dot); }
.interpretation-factors td.negative { color: var(--brand); }
.model-version span { margin-left: 8px; overflow-wrap: anywhere; font-family: inherit; font-size: 12px; font-weight: 400; letter-spacing: 0; color: var(--ink-2); }
.telemetry-statistics { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); background: var(--card-2); border: 1px solid var(--line); margin-bottom: 12px; }
.telemetry-statistics > div { padding: 11px 14px; }
.telemetry-statistics > div + div { border-left: 1px solid var(--line); }
.telemetry-statistics span { display: block; font-size: 11px; color: var(--ink-3); margin-bottom: 5px; }
.telemetry-statistics strong { display: block; font-size: 18px; color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
@media(max-width: 540px) { .reading-heading { align-items: baseline; } .reading-heading > span { font-size: 10px; } .conclusion-points li { padding: 12px; grid-template-columns: 22px minmax(0,1fr); gap: 4px; } .interpretation-factors th, .interpretation-factors td { padding: 9px 5px; } .telemetry-statistics > div { padding: 10px 8px; } .telemetry-statistics strong { font-size: 16px; } }
</style>
