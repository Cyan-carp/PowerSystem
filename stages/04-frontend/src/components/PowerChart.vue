<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { n } from '../lib/format'

const props = withDefaults(
  defineProps<{
    points: [number, number][]
    height?: number
    unit?: string
    name?: string
  }>(),
  { height: 248, unit: 'kW', name: '有功功率' },
)

const wrap = ref<HTMLElement | null>(null)
const width = ref(880)
const hoverIndex = ref<number | null>(null)
let observer: ResizeObserver | null = null

const pad = { top: 16, right: 14, bottom: 28, left: 48 }

onMounted(() => {
  if (!wrap.value) return
  observer = new ResizeObserver((entries) => {
    const next = entries[0]?.contentRect.width
    if (next) width.value = next
  })
  observer.observe(wrap.value)
  width.value = wrap.value.clientWidth || 880
})

onBeforeUnmount(() => observer?.disconnect())

const plotW = computed(() => Math.max(160, width.value - pad.left - pad.right))
const plotH = computed(() => Math.max(80, props.height - pad.top - pad.bottom))

/** 轴上限与步长都取 1 / 2 / 2.5 / 5 的整十倍，刻度才会落在整数上。 */
const scale = computed(() => {
  const peak = Math.max(0, ...props.points.map(([, value]) => value))
  if (!peak) return { max: 100, step: 25 }
  const target = peak * 1.05
  const rough = target / 4
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalized = rough / magnitude
  const step =
    (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10) * magnitude
  return { max: Math.ceil(target / step) * step, step }
})

const coords = computed(() =>
  props.points.map(([ts, value], index) => {
    const total = Math.max(1, props.points.length - 1)
    return {
      ts,
      value,
      x: pad.left + (index / total) * plotW.value,
      y: pad.top + plotH.value - (value / scale.value.max) * plotH.value,
    }
  }),
)

const linePath = computed(() =>
  coords.value.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' '),
)

const areaPath = computed(() => {
  const list = coords.value
  if (!list.length) return ''
  const base = pad.top + plotH.value
  return `${linePath.value} L${list[list.length - 1].x.toFixed(2)} ${base} L${list[0].x.toFixed(2)} ${base} Z`
})

const yTicks = computed(() => {
  const { max, step } = scale.value
  const count = Math.max(1, Math.round(max / step))
  return Array.from({ length: count + 1 }, (_, index) => {
    const value = index * step
    return {
      y: pad.top + plotH.value - (value / max) * plotH.value,
      label: value >= 1000 ? `${n(value / 1000, value % 1000 === 0 ? 0 : 1)}k` : n(value, 0),
    }
  })
})

const timeLabel = (ts: number) =>
  new Date(ts).toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  })

const xTicks = computed(() => {
  const list = coords.value
  if (list.length < 2) return []
  const count = Math.min(6, Math.max(2, Math.floor(plotW.value / 130)))
  return Array.from({ length: count }, (_, index) => {
    const at = Math.round((index / (count - 1)) * (list.length - 1))
    return { x: list[at].x, label: timeLabel(list[at].ts) }
  })
})

const lastPoint = computed(() => coords.value[coords.value.length - 1] || null)

const active = computed(() => (hoverIndex.value == null ? null : coords.value[hoverIndex.value] || null))

const tooltipStyle = computed(() => {
  if (!active.value) return {}
  const left = Math.min(Math.max(active.value.x, 64), width.value - 64)
  return { left: `${left}px`, top: `${Math.max(active.value.y - 12, 8)}px` }
})

const onMove = (event: PointerEvent) => {
  const svg = event.currentTarget as SVGElement
  const rect = svg.getBoundingClientRect()
  const offset = event.clientX - rect.left
  const total = Math.max(1, coords.value.length - 1)
  const ratio = (offset - pad.left) / plotW.value
  hoverIndex.value = Math.max(0, Math.min(coords.value.length - 1, Math.round(ratio * total)))
}

const gradientId = `power-fill-${Math.random().toString(36).slice(2, 8)}`
</script>

<template>
  <div ref="wrap" class="chart" :style="{ height: `${height}px` }">
    <p v-if="!points.length" class="chart-empty">暂无采样点</p>
    <template v-else>
      <svg
        :width="width"
        :height="height"
        :viewBox="`0 0 ${width} ${height}`"
        role="img"
        :aria-label="`${name}随时间变化曲线`"
        @pointermove="onMove"
        @pointerleave="hoverIndex = null"
      >
        <defs>
          <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#2563eb" stop-opacity="0.2" />
            <stop offset="70%" stop-color="#2563eb" stop-opacity="0.03" />
            <stop offset="100%" stop-color="#2563eb" stop-opacity="0" />
          </linearGradient>
        </defs>

        <!-- 辅助线：横竖各一套虚线，读点时能直接顺到坐标轴 -->
        <g class="grid">
          <line
            v-for="tick in yTicks"
            :key="`gy-${tick.y}`"
            :x1="pad.left"
            :x2="width - pad.right"
            :y1="tick.y"
            :y2="tick.y"
          />
          <line
            v-for="tick in xTicks"
            :key="`gx-${tick.x}`"
            :x1="tick.x"
            :x2="tick.x"
            :y1="pad.top"
            :y2="pad.top + plotH"
          />
        </g>

        <!-- y 轴 -->
        <g class="axis">
          <text v-for="tick in yTicks" :key="`y-${tick.y}`" :x="pad.left - 10" :y="tick.y + 3.5" text-anchor="end">
            {{ tick.label }}
          </text>
        </g>

        <!-- x 轴 -->
        <g class="axis">
          <text v-for="tick in xTicks" :key="`x-${tick.x}`" :x="tick.x" :y="height - 8" text-anchor="middle">
            {{ tick.label }}
          </text>
        </g>

        <path :d="areaPath" :fill="`url(#${gradientId})`" stroke="none" />
        <path class="line" :d="linePath" fill="none" />

        <!-- 当前点 -->
        <g v-if="lastPoint" class="head">
          <circle :cx="lastPoint.x" :cy="lastPoint.y" r="7" class="halo" />
          <circle :cx="lastPoint.x" :cy="lastPoint.y" r="3.2" class="dot" />
        </g>

        <!-- 悬停 -->
        <g v-if="active" class="probe">
          <line :x1="active.x" :x2="active.x" :y1="pad.top" :y2="pad.top + plotH" />
          <circle :cx="active.x" :cy="active.y" r="3.6" class="dot" />
        </g>
      </svg>

      <div v-if="active" class="tip" :style="tooltipStyle">
        <span class="tip-time num">{{ timeLabel(active.ts) }}</span>
        <span class="tip-value num">{{ n(active.value, 1) }} <em>{{ unit }}</em></span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.chart {
  position: relative;
  width: 100%;
  min-width: 0;
}

.chart svg {
  display: block;
  width: 100%;
  overflow: visible;
}

.grid line {
  /* 辅助线要看得见：浅灰虚线在浅底上几乎消失，压到 #cdd6e4 */
  stroke: #ccd5e3;
  stroke-width: 1;
  stroke-dasharray: 3 4;
}

.axis text {
  fill: #2b3546;
  font-size: 12px;
  font-family: var(--font);
  font-variant-numeric: tabular-nums;
}

.line {
  stroke: var(--brand);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.head .halo {
  fill: rgba(37, 99, 235, 0.14);
}

.head .dot {
  fill: var(--brand);
  stroke: #fff;
  stroke-width: 1.6;
}

.probe line {
  stroke: #93aeea;
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

.probe .dot {
  fill: var(--brand);
  stroke: #fff;
  stroke-width: 1.6;
}

.tip {
  position: absolute;
  transform: translate(-50%, -100%);
  background: var(--ink);
  color: #fff;
  border-radius: 5px;
  padding: 6px 9px;
  pointer-events: none;
  display: flex;
  flex-direction: column;
  gap: 1px;
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.22);
  white-space: nowrap;
  z-index: 3;
}

.tip-time {
  font-size: 10px;
  color: rgba(255, 255, 255, 0.62);
}

.tip-value {
  font-size: 12px;
  font-weight: 650;
}

.tip-value em {
  font-style: normal;
  font-size: 10px;
  color: rgba(255, 255, 255, 0.62);
  margin-left: 2px;
}

.chart-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 12.5px;
  color: var(--ink-3);
  background: var(--card-soft);
  border-radius: var(--r-md);
}
</style>
