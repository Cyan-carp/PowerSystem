<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { CalibrationBucket } from '../lib/api'
import { n, pct } from '../lib/format'

/**
 * 校准曲线：横轴是预测概率（取桶的中点），纵轴是桶内真实故障占比。
 * 完全校准的点落在 45° 对角线上；点在对角线下方 = 模型高估风险，上方 = 低估。
 * 图不引图表库，宽度按容器测出来，字才不会随容器缩放糊成一片。
 */
const props = withDefaults(
  defineProps<{ buckets: CalibrationBucket[]; ece: number | null; pending?: boolean }>(),
  { pending: false },
)

const wrap = ref<HTMLElement | null>(null)
const width = ref(560)
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!wrap.value) return
  observer = new ResizeObserver((entries) => {
    const next = entries[0]?.contentRect.width
    if (next) width.value = next
  })
  observer.observe(wrap.value)
  width.value = wrap.value.clientWidth || 560
})

onBeforeUnmount(() => observer?.disconnect())

const pad = { top: 18, right: 16, bottom: 42, left: 46 }
/** 绘图区强制正方形：45° 参考线只有宽高相等时才真正是 45°，否则视觉斜率失真 */
const side = computed(() => {
  const available = Math.max(120, width.value - pad.left - pad.right)
  return Math.round(Math.min(available, 400))
})
/** 正方形在容器内水平居中，宽屏下不会被拉成长条 */
const originX = computed(() => pad.left + Math.max(0, (width.value - pad.left - pad.right - side.value) / 2))
const svgH = computed(() => pad.top + side.value + pad.bottom)

const xAt = (value: number) => originX.value + Math.min(1, Math.max(0, value)) * side.value
const yAt = (value: number) => pad.top + side.value - Math.min(1, Math.max(0, value)) * side.value

/** 空桶（count = 0）不画点：没有样本就没有发生率，不能当成 0 */
const points = computed(() => {
  const list: { mid: number; rate: number; count: number }[] = []
  for (const bucket of props.buckets) {
    if (bucket.count <= 0 || bucket.actual_rate == null) continue
    list.push({ mid: (bucket.lower + bucket.upper) / 2, rate: bucket.actual_rate, count: bucket.count })
  }
  return list.sort((left, right) => left.mid - right.mid)
})

const linePath = computed(() =>
  points.value
    .map((point, index) => `${index ? 'L' : 'M'}${xAt(point.mid).toFixed(2)} ${yAt(point.rate).toFixed(2)}`)
    .join(' '),
)

const perfectPath = computed(() => `M${xAt(0)} ${yAt(0)} L${xAt(1)} ${yAt(1)}`)

const ticks = [0, 0.25, 0.5, 0.75, 1]
const tickLabel = (value: number) => pct(value * 100, 0)

/** 有样本的桶占全部桶的比例，用来说明曲线覆盖了几段概率区间 */
const coveredText = computed(() => `${points.value.length} / ${props.buckets.length} 个桶有样本`)
</script>

<template>
  <div class="cal">
    <!-- 量的是这块无内边距的容器：外层留白会把 clientWidth 撑大，SVG 按它排版会偏 -->
    <div ref="wrap" class="cal-plot">
      <p v-if="!points.length" class="cal-empty">
        {{ pending ? '正在计算校准曲线…' : '暂无校准样本：窗口内还没有到期的预测。' }}
      </p>
      <svg
        v-else
        :width="width"
        :height="svgH"
        :viewBox="`0 0 ${width} ${svgH}`"
        role="img"
        aria-label="预测概率与实际发生率的校准曲线"
      >
        <g class="grid">
          <line v-for="tick in ticks" :key="`gy-${tick}`" :x1="originX" :x2="originX + side" :y1="yAt(tick)" :y2="yAt(tick)" />
          <line v-for="tick in ticks" :key="`gx-${tick}`" :x1="xAt(tick)" :x2="xAt(tick)" :y1="pad.top" :y2="pad.top + side" />
        </g>

        <!-- 45° 参考线：完全校准的模型应当贴着它 -->
        <path class="perfect" :d="perfectPath" fill="none" />

        <path class="line" :d="linePath" fill="none" />

        <g class="dot">
          <circle v-for="(point, index) in points" :key="index" :cx="xAt(point.mid)" :cy="yAt(point.rate)" r="3.4" />
        </g>

        <g class="axis">
          <text v-for="tick in ticks" :key="`y-${tick}`" :x="originX - 8" :y="yAt(tick) + 3.5" text-anchor="end">
            {{ tickLabel(tick) }}
          </text>
          <text v-for="tick in ticks" :key="`x-${tick}`" :x="xAt(tick)" :y="svgH - 20" text-anchor="middle">
            {{ tickLabel(tick) }}
          </text>
        </g>

        <text class="axis-title" :x="originX" :y="10">实际发生率</text>
        <text class="axis-title" :x="originX + side" :y="svgH - 4" text-anchor="end">预测概率</text>
      </svg>
    </div>

    <p class="cal-foot">
      <span>ECE <b class="num">{{ ece == null ? '—' : n(ece, 3) }}</b></span>
      <span>虚线为完全校准参考线（预测概率 = 实际发生率）</span>
      <span>{{ coveredText }}</span>
    </p>
  </div>
</template>

<style scoped>
.cal {
  padding: 0 18px 14px;
}

.cal-plot {
  position: relative;
  min-height: 120px;
}

.cal-plot svg {
  display: block;
  width: 100%;
  overflow: visible;
}

.grid line {
  stroke: var(--line);
  stroke-width: 1;
  stroke-dasharray: 3 4;
}

.axis text {
  fill: var(--ink-3);
  font-size: 11px;
  font-family: var(--font);
  font-variant-numeric: tabular-nums;
}

.axis-title {
  fill: var(--ink-4);
  font-size: 10.5px;
  font-family: var(--font);
}

.perfect {
  stroke: var(--ink-4);
  stroke-width: 1;
  stroke-dasharray: 5 4;
  opacity: 0.55;
}

.line {
  stroke: var(--brand);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.dot circle {
  fill: var(--brand);
  stroke: var(--card);
  stroke-width: 1.4;
}

.cal-foot {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--ink-3);
}

.cal-foot b {
  color: var(--ink);
  font-weight: 700;
}

.cal-empty {
  padding: 26px 0 30px;
  text-align: center;
  font-size: 12px;
  color: var(--ink-3);
}
</style>
