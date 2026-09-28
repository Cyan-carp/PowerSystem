<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { Metric } from '../types'
import { metricLabels, metricUnits } from '../lib/format'

const props = defineProps<{ points: [number, number][]; metric: Metric; height?: string }>()
const target = ref<HTMLElement | null>(null)
use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])
let chart: EChartsType | null = null
let resize: ResizeObserver | null = null

function draw(): void {
  if (!chart) return
  chart.setOption({
    animationDurationUpdate: 250,
    grid: { left: 58, right: 28, top: 24, bottom: 42 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (value: unknown) => `${Number(value).toFixed(2)} ${metricUnits[props.metric]}` },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: '#9db2bd' } }, axisLabel: { color: '#66808e' }, splitLine: { show: false } },
    yAxis: { type: 'value', name: metricUnits[props.metric], nameTextStyle: { color: '#66808e' }, axisLabel: { color: '#66808e' }, splitLine: { lineStyle: { color: '#e8eef0' } } },
    series: [{ name: metricLabels[props.metric], type: 'line', smooth: false, showSymbol: false, data: props.points, lineStyle: { color: '#087f78', width: 2.5 }, itemStyle: { color: '#087f78' }, areaStyle: { color: 'rgba(8,127,120,0.08)' } }],
  }, true)
}

onMounted(() => {
  if (!target.value) return
  chart = init(target.value)
  resize = new ResizeObserver(() => chart?.resize())
  resize.observe(target.value)
  draw()
})
watch(() => [props.points, props.metric], draw, { deep: true })
onUnmounted(() => { resize?.disconnect(); chart?.dispose() })
</script>

<template><div ref="target" class="telemetry-chart" :style="{ height: height || '320px' }" role="img" :aria-label="`${metricLabels[metric]}历史曲线，共 ${points.length} 个绘图点`" /></template>
