<script setup lang="ts">
import { computed } from 'vue'
import type { RiskBand } from '../lib/api'

const props = defineProps<{
  bands: RiskBand[]
  samples: number
  faults: number
  baselineRate: number
}>()

/**
 * 累积增益曲线：横轴是「抽查多少比例的设备」，纵轴是「能抓到多少比例的故障」。
 *
 * 不用「前 1% / 前 10%」的说法，是因为分位是统计术语，看的人要先换算成
 * 「检查多少台」才有感觉。换成抽查比例后，一句话就能说清：
 * 抽查 10% 的设备，能抓到九成的故障。
 */

const PAD = { left: 48, right: 16, top: 14, bottom: 36 }
const BOX = { w: 520, h: 300 }
const PLOT = {
  w: BOX.w - PAD.left - PAD.right,
  h: BOX.h - PAD.top - PAD.bottom,
}

const sx = (pct: number) => PAD.left + (pct / 100) * PLOT.w
const sy = (gain: number) => PAD.top + (1 - gain / 100) * PLOT.h

/** 曲线点：从原点出发，按每个抽查比例累加已抓到的故障，最后补到 (100,100) */
const points = computed(() => {
  const total = props.faults || 1
  const result: { x: number; y: number; pct: number }[] = [{ x: sx(0), y: sy(0), pct: 0 }]
  for (const band of props.bands) {
    result.push({
      x: sx(band.pct * 100),
      y: sy((band.faults / total) * 100),
      pct: band.pct * 100,
    })
  }
  result.push({ x: sx(100), y: sy(100), pct: 100 })
  return result
})

const polyline = computed(() => points.value.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '))

/** 关键读数：抽查 10% 时抓到多少。这个数字比整条曲线更容易记住 */
const atTen = computed(() => {
  const total = props.faults || 1
  const band = props.bands.find((b) => Math.abs(b.pct - 0.1) < 1e-6)
  if (!band) return null
  return { gain: Math.round((band.faults / total) * 100), hit: band.hit_rate, lift: band.lift }
})

const ticks = [0, 25, 50, 75, 100]
</script>

<template>
  <div class="gain">
    <div v-if="!bands.length" class="gain-empty">暂无可统计的预测结果</div>

    <svg v-else :viewBox="`0 0 ${BOX.w} ${BOX.h}`" class="gain-svg" role="img"
         aria-label="累积增益曲线：抽查设备比例与抓到故障比例的关系">
      <!-- 网格与刻度 -->
      <g class="grid">
        <line v-for="t in ticks" :key="`gx${t}`" :x1="sx(t)" :y1="PAD.top" :x2="sx(t)" :y2="PAD.top + PLOT.h" />
        <line v-for="t in ticks" :key="`gy${t}`" :x1="PAD.left" :y1="sy(t)" :x2="PAD.left + PLOT.w" :y2="sy(t)" />
      </g>

      <!-- 坐标轴 -->
      <line class="axis" :x1="PAD.left" :y1="PAD.top + PLOT.h" :x2="PAD.left + PLOT.w" :y2="PAD.top + PLOT.h" />
      <line class="axis" :x1="PAD.left" :y1="PAD.top" :x2="PAD.left" :y2="PAD.top + PLOT.h" />

      <!-- 刻度标签 -->
      <text v-for="t in ticks" :key="`tx${t}`" class="tick" :x="sx(t)" :y="PAD.top + PLOT.h + 16" text-anchor="middle">
        {{ t }}%
      </text>
      <text v-for="t in ticks" :key="`ty${t}`" class="tick" :x="PAD.left - 8" :y="sy(t) + 4" text-anchor="end">
        {{ t }}%
      </text>

      <!-- 轴标题 -->
      <text class="axis-label" :x="PAD.left + PLOT.w / 2" :y="BOX.h - 4" text-anchor="middle">抽查设备比例</text>
      <text class="axis-label" :x="14" :y="PAD.top + PLOT.h / 2" text-anchor="middle"
            :transform="`rotate(-90 14 ${PAD.top + PLOT.h / 2})`">抓到故障比例</text>

      <!-- 随机挑选基准线 -->
      <line class="diagonal" :x1="sx(0)" :y1="sy(0)" :x2="sx(100)" :y2="sy(100)" />

      <!-- 模型曲线 -->
      <polyline class="curve" :points="polyline" />

      <!-- 各分位圆点 -->
      <circle v-for="(p, i) in points.slice(1, -1)" :key="`p${i}`" class="dot" :cx="p.x" :cy="p.y" r="3" />

      <!-- 抽查 10% 处的强调 -->
      <template v-if="atTen">
        <line class="mark" :x1="sx(10)" :y1="sy(0)" :x2="sx(10)" :y2="sy(atTen.gain)" />
        <circle class="dot is-key" :cx="sx(10)" :cy="sy(atTen.gain)" r="4" />
      </template>
    </svg>

    <div v-if="atTen" class="gain-readout">
      <span class="readout-main">
        抽查 <b class="num">10%</b> 的设备，能抓到 <b class="num">{{ atTen.gain }}%</b> 的故障
      </span>
      <span class="readout-sub">
        这段命中率 {{ atTen.hit }}%，是随机抽查的 {{ atTen.lift }} 倍
      </span>
    </div>

    <div class="gain-legend">
      <span class="legend-item"><i class="line-model"></i>按模型排序抽查</span>
      <span class="legend-item"><i class="line-random"></i>随机抽查</span>
    </div>
  </div>
</template>

<style scoped>
.gain {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.gain-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.gain-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
}

.gain-sub {
  font-size: 11px;
  color: var(--ink-3);
}

.gain-svg {
  width: 100%;
  height: auto;
  /* 图表不需要占满整张卡片：高度封顶后，读数和图例还在首屏内 */
  max-height: 250px;
  display: block;
}

.grid line {
  stroke: var(--line-soft);
  stroke-width: 1;
}

.axis {
  stroke: var(--line-strong);
  stroke-width: 1;
}

.tick {
  font-size: 9.5px;
  fill: var(--ink-4);
}

.axis-label {
  font-size: 10px;
  fill: var(--ink-3);
}

.diagonal {
  stroke: var(--ink-4);
  stroke-width: 1;
  stroke-dasharray: 4 4;
  opacity: 0.7;
}

.curve {
  fill: none;
  stroke: var(--brand);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.dot {
  fill: var(--brand);
}

.dot.is-key {
  fill: var(--bg);
  stroke: var(--brand);
  stroke-width: 2;
}

.mark {
  stroke: var(--brand);
  stroke-width: 1;
  stroke-dasharray: 2 3;
  opacity: 0.55;
}

.gain-readout {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  padding: 6px 0 2px;
}

.readout-main {
  font-size: 12px;
  color: var(--ink);
}

.readout-main b {
  font-size: 14px;
  color: var(--brand);
  margin: 0 2px;
}

.readout-sub {
  font-size: 10.5px;
  color: var(--ink-3);
}

.gain-legend {
  display: flex;
  gap: 14px;
  font-size: 10.5px;
  color: var(--ink-3);
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.line-model {
  width: 14px;
  height: 2px;
  background: var(--brand);
  border-radius: 1px;
}

.line-random {
  width: 14px;
  height: 0;
  border-top: 1px dashed var(--ink-4);
}

.gain-empty {
  font-size: 11.5px;
  color: var(--ink-3);
  padding: 12px 0;
}
</style>
