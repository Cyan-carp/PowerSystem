<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { GaugeChart } from 'echarts/charts'
import { CanvasRenderer } from 'echarts/renderers'

const props = defineProps<{ value: number | null; name: string; unit: string; floor?: number }>()
const reading = computed(() => props.value !== null && Number.isFinite(props.value) ? props.value : null)
const target = ref<HTMLElement | null>(null)
use([GaugeChart, CanvasRenderer])
let chart: EChartsType | null = null
let resize: ResizeObserver | null = null
let scaleMax = 0

/** 量程只增不减并留 25% 余量，避免数字增长时频繁跳刻度 */
function resolveMax(value: number): number {
  const wanted = Math.max(value * 1.25, props.floor ?? 100)
  if (wanted <= scaleMax) return scaleMax
  const pow = 10 ** Math.floor(Math.log10(wanted))
  const unit = pow / 2
  scaleMax = Math.ceil(wanted / unit) * unit
  return scaleMax
}

function draw(): void {
  if (!chart) return
  const hasValue = reading.value !== null
  const value = reading.value ?? 0
  chart.setOption({
    series: [{
      type: 'gauge',
      startAngle: 210,
      endAngle: -30,
      min: 0,
      max: resolveMax(value),
      radius: '100%',
      center: ['50%', '64%'],
      progress: {
        // Keep progress initialized even before the first sample: ECharts 6.1
        // otherwise reads an absent previous progress array on null → number.
        show: true,
        width: 9,
        roundCap: false,
        itemStyle: { color: '#006eff' },
      },
      axisLine: { roundCap: false, lineStyle: { width: 9, color: [[1, '#f2f3f5']] } },
      pointer: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      title: { show: false },
      detail: {
        valueAnimation: true,
        offsetCenter: [0, '-16%'],
        color: '#1d2129',
        fontSize: 26,
        fontWeight: 800,
        fontFamily: '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
        formatter: (v: number) => (hasValue ? v.toFixed(1) : '—'),
      },
      data: [{ value }],
    }],
  }, true)
}

onMounted(() => {
  if (!target.value) return
  chart = init(target.value)
  resize = new ResizeObserver(() => chart?.resize())
  resize.observe(target.value)
  draw()
})
watch(() => props.value, draw)
onUnmounted(() => { resize?.disconnect(); chart?.dispose() })
</script>

<template>
  <div ref="target" class="metric-gauge" role="img" :aria-label="`${name}，当前 ${reading === null ? '暂无数据' : reading.toFixed(1) + unit}`" />
</template>

<style scoped>
.metric-gauge { width: 100%; height: 168px; }
</style>
