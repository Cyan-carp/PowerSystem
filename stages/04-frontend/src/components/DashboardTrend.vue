<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { DailyPoint } from '../lib/daily-generation'

const props = defineProps<{ points: DailyPoint[]; kind: 'power' | 'energy' }>()
const target = ref<HTMLElement | null>(null)
use([BarChart, LineChart, GridComponent, TooltipComponent, CanvasRenderer])
let chart: EChartsType | null = null
let resize: ResizeObserver | null = null
const integer = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })

function draw(): void {
  if (!chart) return
  const power = props.kind === 'power'
  const values = props.points.flatMap(([, value]) => value != null && Number.isFinite(value) ? [value] : [])
  const maximum = Math.max(1, ...values)
  const magnitude = 10 ** Math.floor(Math.log10(maximum / 5))
  const step = Math.max(1, [1, 2, 5, 10].map((n) => n * magnitude).find((n) => n >= maximum / 5) ?? 1)
  chart.setOption({
    animation: false,
    grid: { left: 52, right: 18, top: 22, bottom: 32 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: power ? 'line' : 'shadow' },
      backgroundColor: '#1e2024', borderColor: '#35383e', textStyle: { color: '#d9dbe0' },
      formatter: (params: unknown) => {
        const item = (Array.isArray(params) ? params[0] : params) as { data?: DailyPoint }
        const point = item?.data
        if (!point || point[1] == null) return '暂无采样'
        const hour = Math.floor(point[0])
        const minute = Math.floor((point[0] - hour) * 60)
        const label = power ? `${hour}:${String(minute).padStart(2, '0')}` : `${hour}:00–${hour + 1}:00`
        return `${label}<br/>${power ? '平均发电功率' : '时段发电量'}：${integer.format(point[1])} ${power ? 'kW' : 'kWh'}`
      },
    },
    xAxis: {
      type: 'value', min: 0, max: 24, interval: 4,
      axisLine: { lineStyle: { color: '#35383e' } },
      axisLabel: { color: '#787d85', fontSize: 11, formatter: (value: number) => `${value}点`, showMinLabel: true, showMaxLabel: true },
      axisTick: { show: false }, splitLine: { show: false },
    },
    yAxis: {
      type: 'value', min: 0, max: Math.ceil(maximum / step) * step, interval: step, minInterval: 1,
      name: power ? 'kW' : 'kWh', nameTextStyle: { color: '#787d85', fontSize: 10 },
      axisLabel: { color: '#787d85', fontSize: 11, formatter: (value: number) => integer.format(value) },
      axisLine: { show: false }, axisTick: { show: false }, splitLine: { lineStyle: { color: '#26282d' } },
    },
    series: [{
      type: power ? 'line' : 'bar', data: props.points,
      ...(power ? { smooth: false, showSymbol: false, connectNulls: false, lineStyle: { color: '#5b8def', width: 2 }, areaStyle: { color: 'rgba(91,141,239,.10)' } }
        : { barWidth: '65%', barMaxWidth: 22, itemStyle: { color: '#5b8def', borderRadius: [2, 2, 0, 0] } }),
    }],
    graphic: values.length ? [] : [{ type: 'text', left: 'center', top: 'middle', style: { text: '今日暂无历史采样', fill: '#787d85', fontSize: 12 } }],
  }, true)
}
onMounted(() => {
  if (!target.value) return
  chart = init(target.value)
  resize = new ResizeObserver(() => chart?.resize())
  resize.observe(target.value)
  draw()
})
watch(() => [props.points, props.kind], draw, { deep: true })
onUnmounted(() => { resize?.disconnect(); chart?.dispose() })
</script>

<template>
  <div ref="target" class="dashboard-trend" role="img" :aria-label="`今日0点至24点${kind === 'power' ? '发电功率折线图' : '每小时发电量柱状图'}，横轴每4小时一个刻度，纵轴为整数`" />
</template>

<style scoped>
.dashboard-trend { width: 100%; height: 210px; }
</style>
