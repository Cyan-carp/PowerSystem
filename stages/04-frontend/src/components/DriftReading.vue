<script setup lang="ts">
import { computed } from 'vue'
import type { DriftEvent } from '../lib/api'
import { n } from '../lib/format'

const props = defineProps<{ event: DriftEvent }>()
const chart = computed(() => {
  const { observed, baseline, threshold, signal } = props.event
  const relative = signal === 'probability'
  const ratio = ['probability', 'high_ratio', 'precision', 'ece', 'recall', 'accuracy', 'f1', 'prob_mean', 'positive_rate'].includes(signal)
  const display = (v: number | null) => v == null ? '—' : ratio ? n(v * 100, 2) + '%' : n(v, 3)
  const marks = [
    { key: 'base', label: '基线', value: baseline },
    { key: 'observed', label: '观测', value: observed },
  ]
  if (relative) {
    if (baseline != null && threshold != null) {
      marks.push({ key: 'limit', label: '下限', value: baseline * (1 - threshold) },
        { key: 'limit', label: '上限', value: baseline * (1 + threshold) })
    }
  } else {
    const label = signal === 'precision' ? '下限' : ['ece', 'high_ratio'].includes(signal) ? '上限' : '阈值'
    marks.push({ key: 'limit', label, value: threshold })
  }
  const values = marks.flatMap(m => m.value == null ? [] : [m.value])
  const low = Math.min(0, ...values)
  const high = Math.max(...values, low + 0.001)
  const position = (value: number) => 4 + (value - low) / (high - low) * 92
  let change = '缺少对照值'
  if (observed != null && baseline != null) {
    const delta = observed - baseline
    const diff = relative && baseline > 0 ? n(Math.abs(delta / baseline) * 100, 1) + '%'
      : ratio ? n(Math.abs(delta) * 100, 2) + ' 个百分点' : n(Math.abs(delta), 3)
    change = delta === 0 ? '与基线持平' : `较基线${delta > 0 ? '上升' : '下降'} ${diff}`
  }
  return { marks, position, display, change, thresholdText: relative ? '相对变化阈值 ' + display(threshold) : '' }
})
</script>
<template>
  <div class="reading">
    <strong>{{ chart.change }}</strong>
    <div class="scale" aria-hidden="true">
      <template v-for="m in chart.marks" :key="m.label">
        <i v-if="m.value != null" :class="m.key" :style="{ left: chart.position(m.value) + '%' }"></i>
      </template>
    </div>
    <div class="keys">
      <span v-for="m in chart.marks" :key="m.label"><i :class="m.key"></i>{{ m.label }} {{ chart.display(m.value) }}</span>
      <span v-if="chart.thresholdText">{{ chart.thresholdText }}</span>
    </div>
  </div>
</template>
<style scoped>
.reading { display:grid; gap:9px; min-width:0; font-variant-numeric:tabular-nums }
.reading strong { font-size:14px; font-weight:650 }
.scale { height:5px; border-radius:4px; background:var(--track-bg); position:relative; margin:3px 0 }
.scale i { position:absolute; top:50%; transform:translate(-50%,-50%); }
.scale .base,.keys .base { width:9px; height:9px; background:var(--ink-2); transform:rotate(45deg) }
.scale .base { transform:translate(-50%,-50%) rotate(45deg) }
.scale .limit,.keys .limit { width:0; height:15px; border-left:2px dashed var(--warn-deep) }
.scale .observed,.keys .observed { width:10px; height:10px; border:2px solid var(--card); border-radius:100%; background:var(--brand); z-index:1; box-shadow:0 0 0 1px var(--brand) }
.keys { display:flex; flex-wrap:wrap; gap:4px 13px; font-size:12px; color:var(--ink-2) }
.keys span { display:inline-flex; align-items:center; gap:6px }
.keys i { display:inline-block; scale:.75 }
</style>
