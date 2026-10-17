<script setup lang="ts">
import { computed } from 'vue'
import { alarmComparison, fixedNumber } from '../lib/detailPresentation'
import { metricUnits } from '../lib/format'
import type { Metric } from '../types'

const props = defineProps<{ value: number | null; threshold: number | null; metric: string }>()
const chart = computed(() => alarmComparison(props.value, props.threshold, props.metric === 'ai_failure_risk'))
const unit = computed(() => metricUnits[props.metric as Metric] || '')
const accessibleText = computed(() => `触发时实测值 ${fixedNumber(props.value)}${unit.value}，阈值 ${fixedNumber(props.threshold)}${unit.value}。${chart.value?.differenceText || '数据不完整，无法绘制对比。'}`)
</script>

<template>
  <section class="alarm-comparison" aria-label="触发时数值对比">
    <div class="comparison-heading"><h3>触发时数值对比</h3><span v-if="chart" class="comparison-difference" :class="{ 'is-above': chart.above }">{{ chart.differenceText }}<template v-if="unit && Math.abs((value ?? 0) - (threshold ?? 0)) >= .01"> {{ unit }}</template></span></div>
    <div class="comparison-values">
      <div class="comparison-actual"><span>实测值<small v-if="metric === 'ai_failure_risk'"> · 模型风险值</small></span><strong :class="{ 'is-above': chart?.above }">{{ fixedNumber(value) }}<small v-if="unit">{{ unit }}</small></strong></div>
      <div class="comparison-threshold"><span>触发阈值</span><strong>{{ fixedNumber(threshold) }}<small v-if="unit">{{ unit }}</small></strong></div>
    </div>
    <div v-if="chart" class="comparison-plot" role="img" :aria-label="accessibleText">
      <div class="comparison-track" aria-hidden="true">
        <span class="comparison-fill" :style="{ left: `${chart.fillStart}%`, width: `${chart.fillWidth}%` }" />
        <span v-if="chart.excessWidth > 0" class="comparison-excess" :style="{ left: `${chart.excessStart}%`, width: `${chart.excessWidth}%` }" />
        <span class="comparison-threshold-marker" :style="{ left: `${chart.limit}%` }" />
        <span class="comparison-actual-marker" :class="{ 'is-above': chart.above }" :style="{ left: `${chart.actual}%` }" />
      </div>
      <div class="comparison-scale" aria-hidden="true"><span>{{ fixedNumber(chart.minimum) }}</span><span>{{ fixedNumber(chart.maximum) }}</span></div>
      <span class="comparison-threshold-label" aria-hidden="true" :style="{ left: `${Math.max(12, Math.min(88, chart.limit))}%` }">阈值 {{ fixedNumber(threshold) }}</span>
    </div>
    <p v-else class="comparison-missing">数值不完整，暂无法绘制对比。</p>
    <p class="comparison-caption">{{ metric === 'ai_failure_risk' ? '模型风险值范围为 0–1。' : '' }}{{ chart?.above ? '红色区间表示高于阈值的部分。' : '竖线标记触发阈值。' }}这里显示触发记录，当前状态以上方标签为准。</p>
  </section>
</template>
