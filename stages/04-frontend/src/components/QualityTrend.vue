<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { PredictionQualityPoint } from '../lib/api'
import { pct } from '../lib/format'

/**
 * 按天趋势：命中率（precision）与漏报率（1 − recall）两条线。
 * 两条线同时看才有意义——只追命中率可以把阈值一路调高，代价全在漏报上。
 * 缺失日期不画点，也不拉直线跨过去。
 */
const props = withDefaults(
  defineProps<{ points: PredictionQualityPoint[]; pending?: boolean; height?: number }>(),
  { pending: false, height: 208 },
)

const wrap = ref<HTMLElement | null>(null)
const width = ref(720)
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!wrap.value) return
  observer = new ResizeObserver((entries) => {
    const next = entries[0]?.contentRect.width
    if (next) width.value = next
  })
  observer.observe(wrap.value)
  width.value = wrap.value.clientWidth || 720
})

onBeforeUnmount(() => observer?.disconnect())

const pad = { top: 14, right: 16, bottom: 26, left: 42 }
const plotW = computed(() => Math.max(160, width.value - pad.left - pad.right))
const plotH = computed(() => Math.max(70, props.height - pad.top - pad.bottom))

const xAt = (index: number) => {
  const total = props.points.length
  if (total <= 1) return pad.left + plotW.value / 2
  return pad.left + (index / (total - 1)) * plotW.value
}
const yAt = (value: number) => pad.top + plotH.value - Math.min(1, Math.max(0, value)) * plotH.value

/** 逐段拼路径：遇到 null 就断开，避免把「没有样本的一天」画成 0% */
const segments = (values: (number | null)[]) => {
  const out: string[] = []
  let current: string[] = []
  values.forEach((value, index) => {
    if (value == null) {
      if (current.length) {
        out.push(current.join(' '))
        current = []
      }
      return
    }
    const command = current.length ? 'L' : 'M'
    current.push(`${command}${xAt(index).toFixed(2)} ${yAt(value).toFixed(2)}`)
  })
  if (current.length) out.push(current.join(' '))
  return out
}

/**
 * 按天趋势：默认画**故障批次口径**——当天被提前叫出的批次 ÷ 当天故障批次。
 * 窗口口径（precision / 1 − recall）留在接口里做参考，但不画：
 * 分母含大量「离故障还早」的窗口，看着永远像 3%，不是运维关心的指标。
 * 当天没有故障时批次分母为 0，线条断开，不画成 0%。
 */
const hitValue = (point: PredictionQualityPoint): number | null =>
  point.batch_rate != null ? point.batch_rate : point.precision
const missValue = (point: PredictionQualityPoint): number | null => {
  if (point.batch_rate != null) return 1 - point.batch_rate
  return point.recall == null ? null : 1 - point.recall
}

const hitPaths = computed(() => segments(props.points.map(hitValue)))
const missPaths = computed(() => segments(props.points.map(missValue)))

const dots = computed(() =>
  props.points
    .map((point, index) => ({
      index,
      hit: hitValue(point),
      miss: missValue(point),
    }))
    .filter((item) => item.hit != null || item.miss != null),
)

const yTicks = [0, 0.25, 0.5, 0.75, 1]

/** day 是 YYYY-MM-DD；只取月日，横轴才放得下 */
const dayLabel = (day: string) => (day.length >= 10 ? `${day.slice(5, 7)}/${day.slice(8, 10)}` : day)

const xTicks = computed(() => {
  const total = props.points.length
  if (!total) return []
  const count = Math.min(5, total)
  return Array.from({ length: count }, (_, index) => {
    const at = count === 1 ? 0 : Math.round((index / (count - 1)) * (total - 1))
    return { x: xAt(at), label: dayLabel(props.points[at].day) }
  })
})
</script>

<template>
  <div class="tr">
    <div class="tr-legend">
      <span class="lg"><i class="dot-hit"></i>批次命中率</span>
      <span class="lg"><i class="dot-miss"></i>批次漏报率</span>
    </div>

    <!-- 量的是这块无内边距的容器：外层留白会把 clientWidth 撑大，SVG 按它排版会偏 -->
    <div ref="wrap" class="tr-plot">
      <p v-if="!points.length" class="tr-empty">
        {{ pending ? '正在统计按天趋势…' : '暂无按天数据：所选窗口内还没有到期的预测。' }}
      </p>
      <svg
        v-else
        :width="width"
        :height="height"
        :viewBox="`0 0 ${width} ${height}`"
        role="img"
        aria-label="近段时间按故障批次的命中率与漏报率趋势"
      >
        <g class="grid">
          <line
            v-for="tick in yTicks"
            :key="`gy-${tick}`"
            :x1="pad.left"
            :x2="width - pad.right"
            :y1="yAt(tick)"
            :y2="yAt(tick)"
          />
        </g>

        <g class="axis">
          <text v-for="tick in yTicks" :key="`y-${tick}`" :x="pad.left - 8" :y="yAt(tick) + 3.5" text-anchor="end">
            {{ pct(tick * 100, 0) }}
          </text>
          <text v-for="tick in xTicks" :key="`x-${tick.x}`" :x="tick.x" :y="height - 8" text-anchor="middle">
            {{ tick.label }}
          </text>
        </g>

        <path v-for="(path, index) in hitPaths" :key="`hit-${index}`" class="line is-hit" :d="path" fill="none" />
        <path v-for="(path, index) in missPaths" :key="`miss-${index}`" class="line is-miss" :d="path" fill="none" />

        <g class="dots">
          <template v-for="item in dots" :key="item.index">
            <circle v-if="item.hit != null" class="is-hit" :cx="xAt(item.index)" :cy="yAt(item.hit)" r="2.6" />
            <circle v-if="item.miss != null" class="is-miss" :cx="xAt(item.index)" :cy="yAt(item.miss)" r="2.6" />
          </template>
        </g>
      </svg>
    </div>
  </div>
</template>

<style scoped>
.tr {
  padding: 0 18px 14px;
}

.tr-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--ink-3);
}

.lg {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.lg i {
  width: 14px;
  height: 2.5px;
  border-radius: 2px;
}

.dot-hit {
  background: var(--brand);
}

.dot-miss {
  background: var(--bad);
}

.tr-plot {
  position: relative;
  min-height: 120px;
}

.tr-plot svg {
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

.line {
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.line.is-hit {
  stroke: var(--brand);
}

.line.is-miss {
  stroke: var(--bad);
}

.dots circle {
  stroke: var(--card);
  stroke-width: 1.2;
}

.dots circle.is-hit {
  fill: var(--brand);
}

.dots circle.is-miss {
  fill: var(--bad);
}

.tr-empty {
  padding: 26px 0 30px;
  text-align: center;
  font-size: 12px;
  color: var(--ink-3);
}
</style>
