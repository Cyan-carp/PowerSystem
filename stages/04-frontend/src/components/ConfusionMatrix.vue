<script setup lang="ts">
import { computed } from 'vue'

/**
 * 混淆矩阵热力图：四格按样本量着色，深浅用对数刻度（长尾分布下线性能分出层次）。
 * 用 CSS Grid 而非 SVG，格子在卡片内按高度铺满，避免卡片被相邻卡撑高后上下留白。
 * 正确判定用 --ok、错误判定用 --bad；颜色之外仍保留数值与语义文字。
 */
const props = defineProps<{ tp: number; fp: number; fn: number; tn: number }>()

const total = computed(() => props.tp + props.fp + props.fn + props.tn)
const max = computed(() => Math.max(props.tp, props.fp, props.fn, props.tn, 1))

interface Cell {
  key: string
  label: string
  note: string
  value: number
  good: boolean
}

const cells = computed<Cell[]>(() => [
  { key: 'tp', label: '命中', note: '预测故障 · 实际故障', value: props.tp, good: true },
  { key: 'fn', label: '漏报', note: '预测正常 · 实际故障', value: props.fn, good: false },
  { key: 'fp', label: '误报', note: '预测故障 · 实际正常', value: props.fp, good: false },
  { key: 'tn', label: '正确拒绝', note: '预测正常 · 实际正常', value: props.tn, good: true },
])

/** 对数刻度：样本极不均衡时，线性映射会让小格完全看不出差异 */
const strength = (value: number) => Math.log1p(Math.max(0, value)) / Math.log1p(max.value)
const background = (cell: Cell) => {
  const mix = Math.round(10 + 78 * strength(cell.value))
  const base = cell.good ? 'var(--ok)' : 'var(--bad)'
  return `color-mix(in srgb, ${base} ${mix}%, var(--card))`
}
const isStrong = (cell: Cell) => strength(cell.value) > 0.62

const fmt = (value: number) => value.toLocaleString('zh-CN')
const pctText = (value: number) => (total.value ? `${((value / total.value) * 100).toFixed(2)}%` : '—')
</script>

<template>
  <div class="cm-figure" role="img" aria-label="混淆矩阵：按样本量着色的四格热力图">
    <span class="cm-corner"></span>
    <span class="cm-axis">预测故障</span>
    <span class="cm-axis">预测正常</span>

    <span class="cm-axis is-side">实际故障</span>
    <div
      v-for="cell in cells.slice(0, 2)"
      :key="cell.key"
      class="cm-cell"
      :class="{ 'is-strong': isStrong(cell) }"
      :style="{ background: background(cell) }"
    >
      <b class="num">{{ fmt(cell.value) }}</b>
      <span class="cm-share num">{{ pctText(cell.value) }}</span>
      <span class="cm-name">{{ cell.label }}</span>
      <em>{{ cell.note }}</em>
    </div>

    <span class="cm-axis is-side">实际正常</span>
    <div
      v-for="cell in cells.slice(2)"
      :key="cell.key"
      class="cm-cell"
      :class="{ 'is-strong': isStrong(cell) }"
      :style="{ background: background(cell) }"
    >
      <b class="num">{{ fmt(cell.value) }}</b>
      <span class="cm-share num">{{ pctText(cell.value) }}</span>
      <span class="cm-name">{{ cell.label }}</span>
      <em>{{ cell.note }}</em>
    </div>
  </div>
</template>

<style scoped>
.cm-figure {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr) minmax(0, 1fr);
  grid-template-rows: auto minmax(88px, 1fr) minmax(88px, 1fr);
  gap: 6px;
  flex: 1;
  min-height: 0;
}

.cm-corner {
  display: block;
}

.cm-axis {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--ink-3);
  padding-bottom: 2px;
}

.cm-axis.is-side {
  justify-content: flex-end;
  padding-right: 8px;
  padding-bottom: 0;
  text-align: right;
  line-height: 1.4;
}

.cm-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1px;
  padding: 10px 8px;
  border-radius: var(--r-md);
  border: 1px solid var(--line-soft);
  text-align: center;
  transition: background 0.2s ease;
}

.cm-cell b {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.15;
  color: var(--ink);
}

.cm-share {
  font-size: 10.5px;
  color: var(--ink-3);
}

.cm-name {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--ink-2);
}

.cm-cell em {
  font-style: normal;
  font-size: 10px;
  color: var(--ink-3);
  line-height: 1.4;
}

/* 深色格子上文字转白，保证对比度 */
.cm-cell.is-strong b,
.cm-cell.is-strong .cm-name {
  color: #fff;
}

.cm-cell.is-strong .cm-share,
.cm-cell.is-strong em {
  color: rgba(255, 255, 255, 0.82);
}
</style>
