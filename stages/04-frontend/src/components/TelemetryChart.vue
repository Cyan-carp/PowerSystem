<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { Metric } from '../types'
import { dateTime, metricLabels, metricUnits } from '../lib/format'

const props = defineProps<{ points: [number, number][]; metric: Metric; height?: string }>()
const target = ref<HTMLElement | null>(null)
use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])
let chart: EChartsType | null = null
let resize: ResizeObserver | null = null
const range = computed(() => props.points.length ? `${dateTime(props.points[0][0])} — ${dateTime(props.points[props.points.length - 1][0])}` : '暂无有效采样')

function draw(): void {
  if (!chart) return
  const width = target.value?.clientWidth || 360
  const span = props.points.length > 1 ? props.points[props.points.length - 1][0] - props.points[0][0] : 0
  const crossDay = props.points.length > 1 && dateTime(props.points[0][0]).split(' ')[0] !== dateTime(props.points[props.points.length - 1][0]).split(' ')[0]
  chart.setOption({
    animationDurationUpdate: 250,
    grid: { left: 54, right: 24, top: 24, bottom: crossDay ? 48 : 32 },
    tooltip: { trigger: 'axis', confine: true, backgroundColor: '#18212d', borderColor: '#32435a', textStyle: { color: '#c8d2df' }, axisPointer: { type: 'line' }, formatter: (params: unknown) => {
      const item = (Array.isArray(params) ? params[0] : params) as { data?: unknown }
      const point = item?.data
      if (!Array.isArray(point) || point.length < 2) return ''
      return `${dateTime(Number(point[0]))}<br/>${metricLabels[props.metric]}：${Number(point[1]).toFixed(2)} ${metricUnits[props.metric]}`
    } },
    xAxis: { type: 'time', splitNumber: Math.max(2, Math.min(5, Math.floor(width / 120))), axisLine: { lineStyle: { color: '#2a3a4c' } }, axisTick: { show: false }, axisLabel: { color: '#8a9bb0', fontSize: 11, hideOverlap: true, margin: 12, formatter: (value: number) => {
      const [day, time] = dateTime(value).split(' ')
      const clock = span >= 5 * 60_000 ? time?.slice(0, 5) : time
      return crossDay ? `${day}\n${clock}` : clock || ''
    } }, splitLine: { show: false } },
    yAxis: { type: 'value', name: metricUnits[props.metric], minInterval: .01, splitNumber: 4, nameTextStyle: { color: '#8a9bb0' }, axisLabel: { color: '#8a9bb0', fontSize: 11, formatter: (value: number) => value.toFixed(2) }, splitLine: { lineStyle: { color: '#24303f' } } },
    series: [{ name: metricLabels[props.metric], type: 'line', smooth: false, showSymbol: false, data: props.points, lineStyle: { color: '#34c9c0', width: 2 }, itemStyle: { color: '#34c9c0' }, areaStyle: { color: 'rgba(52,201,192,0.12)' } }],
  }, true)
}

onMounted(() => {
  if (!target.value) return
  chart = init(target.value)
  resize = new ResizeObserver(() => { chart?.resize(); draw() })
  resize.observe(target.value)
  draw()
})
watch(() => [props.points, props.metric], draw, { deep: true })
onUnmounted(() => { resize?.disconnect(); chart?.dispose() })
</script>

<template><div class="telemetry-frame"><p class="telemetry-range">{{ range }}<span>北京时间</span></p><div ref="target" class="telemetry-chart" :style="{ height: height || '320px' }" role="img" :aria-label="`${metricLabels[metric]}历史曲线，共 ${points.length} 个绘图点，时间轴自动稀疏并隐藏重叠标签，数值保留两位小数`" /></div></template>

<style scoped>
.telemetry-frame { min-width: 0; }
.telemetry-range { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; margin: 0 0 4px; color: var(--ink-3); font-size: 11px; line-height: 1.6; font-variant-numeric: tabular-nums; }
.telemetry-range span { font-size: 10px; }
.telemetry-chart { width: 100%; min-width: 0; }
</style>
