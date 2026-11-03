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
 * 条宽用「相对最前一段」的比例，而不是绝对命中率。
 * 正样本稀疏时命中率全是 0.x%，按绝对比例画每根条都只有一两像素；
 * 相对最前一段能把几段之间的差异拉开，读者看的是排序而不是绝对值。
 */
const maxHitRate = computed(() => Math.max(...props.bands.map((b) => b.hit_rate), 0.0001))

const widthOf = (band: RiskBand) => {
  if (band.samples === 0) return '0%'
  const ratio = band.hit_rate / maxHitRate.value
  // 最矮也留 4%，否则「命中 0 次」的段会完全消失，看不出它被统计过
  return `${Math.max(ratio * 100, band.faults > 0 ? 6 : 4).toFixed(1)}%`
}

const liftText = (band: RiskBand) => {
  if (band.faults === 0) return '未命中'
  return `提升 ${band.lift}×`
}
</script>

<template>
  <div class="risk-bands">
    <div class="bands-head">
      <span class="bands-title">高风险组实际命中率</span>
      <span class="bands-sub">
        全部 {{ samples }} 条 · 真实故障 {{ faults }} 次 · 全局故障率 {{ baselineRate }}%
      </span>
    </div>

    <div v-if="!bands.length" class="bands-empty">暂无可统计的预测结果</div>

    <div v-else class="bands-body">
      <div v-for="band in bands" :key="band.pct" class="band-row">
        <span class="band-label">{{ band.label }}</span>
        <div class="band-track">
          <div class="band-fill" :class="{ 'is-hit': band.faults > 0 }" :style="{ width: widthOf(band) }"></div>
        </div>
        <span class="band-rate num">{{ band.hit_rate }}%</span>
        <span class="band-meta">
          {{ band.samples }} 条 · 命中 {{ band.faults }} · {{ liftText(band) }}
        </span>
      </div>
    </div>

    <p class="bands-foot">
      读法：取预测概率最高的前 N% 记录，看里面真实故障占多少。「提升」= 段内命中率 ÷ 全局故障率，
      等于 1 说明和随便挑没区别。
    </p>
  </div>
</template>

<style scoped>
.risk-bands {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.bands-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.bands-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
}

.bands-sub {
  font-size: 11px;
  color: var(--ink-3);
}

.bands-body {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.band-row {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr) 52px 132px;
  align-items: center;
  gap: 8px;
}

.band-label {
  font-size: 11.5px;
  color: var(--ink-2);
}

.band-track {
  height: 14px;
  border-radius: 3px;
  background: var(--line-soft);
  overflow: hidden;
}

.band-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--ink-4);
  transition: width 0.25s ease;
}

.band-fill.is-hit {
  background: var(--brand);
}

.band-rate {
  font-size: 12px;
  font-weight: 600;
  text-align: right;
  color: var(--ink);
}

.band-meta {
  font-size: 10.5px;
  color: var(--ink-3);
}

.bands-empty {
  font-size: 11.5px;
  color: var(--ink-3);
  padding: 12px 0;
}

.bands-foot {
  margin: 2px 0 0;
  font-size: 10.5px;
  line-height: 1.65;
  color: var(--ink-4);
}

@media (max-width: 1180px) {
  .band-row {
    grid-template-columns: 46px minmax(0, 1fr) 46px;
  }

  .band-meta {
    display: none;
  }
}
</style>
