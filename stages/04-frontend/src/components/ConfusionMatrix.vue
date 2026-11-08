<script setup lang="ts">
import { computed } from 'vue'

/**
 * 混淆矩阵：2×2 正方形热力格 + 右侧三条派生指标。
 *
 * 早先是 4 个铺满卡片宽度的长方形色块，看上去像 4 张卡片而不是矩阵；
 * 现在固定 84px 方块、行列各带轴标签，形状回到标准混淆矩阵的样子。
 * 深浅仍用对数刻度（长尾分布下线性能分出层次），好坏由位置与文字承担，不用红绿双色。
 *
 * 右侧补的召回率 / 精确率 / 准确率是这四格最常被问的三个派生值，
 * 分母写进 title，避免读者自己去推。
 */
const props = withDefaults(
  defineProps<{ tp: number; fp: number; fn: number; tn: number; compact?: boolean }>(),
  { compact: false },
)

const total = computed(() => props.tp + props.fp + props.fn + props.tn)
const max = computed(() => Math.max(props.tp, props.fp, props.fn, props.tn, 1))

interface Cell {
  key: string
  label: string
  value: number
  hit: boolean
}

const cells = computed<Cell[]>(() => [
  { key: 'tp', label: '命中', value: props.tp, hit: true },
  { key: 'fn', label: '漏报', value: props.fn, hit: false },
  { key: 'fp', label: '误报', value: props.fp, hit: false },
  { key: 'tn', label: '正确拒绝', value: props.tn, hit: true },
])

/** 对数刻度：样本极不均衡时，线性映射会让小格完全看不出差异 */
const strength = (value: number) => Math.log1p(Math.max(0, value)) / Math.log1p(max.value)
const mixOf = (value: number) => Math.round(8 + 74 * strength(value))
const background = (cell: Cell) => `color-mix(in srgb, var(--brand) ${mixOf(cell.value)}%, var(--card))`
const isStrong = (cell: Cell) => mixOf(cell.value) > 52

const fmt = (value: number) => value.toLocaleString('zh-CN')
const pctText = (value: number) => (total.value ? `${((value / total.value) * 100).toFixed(2)}%` : '—')
const ratioText = (value: number) => `${(value * 100).toFixed(1)}%`

const metrics = computed(() => {
  const { tp, fp, fn, tn } = props
  const recall = tp + fn > 0 ? tp / (tp + fn) : null
  const precision = tp + fp > 0 ? tp / (tp + fp) : null
  const accuracy = total.value > 0 ? (tp + tn) / total.value : null
  return [
    {
      label: '召回率',
      value: recall == null ? '—' : ratioText(recall),
      hint: `实际故障里被叫出的比例：${fmt(tp)} ÷ (${fmt(tp)} + ${fmt(fn)})`,
      pct: recall == null ? 0 : recall * 100,
      color: 'var(--ok)',
    },
    {
      label: '精确率',
      value: precision == null ? '—' : ratioText(precision),
      hint: `报出来的里面真故障的比例：${fmt(tp)} ÷ (${fmt(tp)} + ${fmt(fp)})`,
      pct: precision == null ? 0 : precision * 100,
      color: 'var(--warn)',
    },
    {
      label: '准确率',
      value: accuracy == null ? '—' : ratioText(accuracy),
      hint: '判对的窗口占全部窗口的比例',
      pct: accuracy == null ? 0 : accuracy * 100,
      color: 'var(--brand)',
    },
  ]
})
</script>

<template>
  <div class="cm" :class="{ 'is-compact': compact }">
    <div class="cm-grid" role="img" aria-label="混淆矩阵：按样本量着色的 2×2 热力图">
      <span class="cm-corner"></span>
      <span class="cm-axis">预测故障</span>
      <span class="cm-axis">预测正常</span>

      <span class="cm-axis is-side">实际<br />故障</span>
      <div
        v-for="cell in cells.slice(0, 2)"
        :key="cell.key"
        class="cm-cell"
        :class="{ 'is-strong': isStrong(cell) }"
        :style="{ background: background(cell) }"
        :title="`${cell.label} ${fmt(cell.value)}（占全部 ${pctText(cell.value)}）`"
      >
        <b class="num">{{ fmt(cell.value) }}</b>
        <span class="cm-share num">{{ pctText(cell.value) }}</span>
        <i class="cm-name">{{ cell.label }}</i>
      </div>

      <span class="cm-axis is-side">实际<br />正常</span>
      <div
        v-for="cell in cells.slice(2)"
        :key="cell.key"
        class="cm-cell"
        :class="{ 'is-strong': isStrong(cell) }"
        :style="{ background: background(cell) }"
        :title="`${cell.label} ${fmt(cell.value)}（占全部 ${pctText(cell.value)}）`"
      >
        <b class="num">{{ fmt(cell.value) }}</b>
        <span class="cm-share num">{{ pctText(cell.value) }}</span>
        <i class="cm-name">{{ cell.label }}</i>
      </div>
    </div>

    <div class="cm-metrics">
      <div v-for="m in metrics" :key="m.label" class="cm-metric" :title="m.hint">
        <span class="cm-metric-label">{{ m.label }}</span>
        <span class="cm-track"><i :style="{ width: `${m.pct}%`, background: m.color }"></i></span>
        <b class="num">{{ m.value }}</b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cm {
  display: flex;
  align-items: center;
  gap: 26px;
  min-width: 0;
}

/* 固定方块 + 行列轴标签：矩阵本来就该是方的，铺满宽度就变成了四张卡片 */
.cm-grid {
  display: grid;
  grid-template-columns: 40px auto auto;
  grid-template-rows: auto auto auto;
  gap: 5px;
  flex: 0 0 auto;
}

.cm-corner {
  display: block;
}

.cm-axis {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10.5px;
  color: var(--ink-3);
  white-space: nowrap;
  padding-bottom: 1px;
}

.cm-axis.is-side {
  justify-content: flex-end;
  padding-right: 6px;
  line-height: 1.35;
  text-align: right;
}

.cm-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1px;
  width: 84px;
  height: 84px;
  padding: 4px;
  border-radius: var(--r-md);
  border: 1px solid var(--line-soft);
  text-align: center;
  transition: background 0.2s ease;
}

.cm-cell b {
  font-size: 17px;
  font-weight: 700;
  line-height: 1.15;
  color: var(--ink);
}

.cm-share {
  font-size: 10px;
  color: var(--ink-3);
}

.cm-name {
  font-style: normal;
  font-size: 10.5px;
  color: var(--ink-2);
  line-height: 1.3;
}

/* 深色格子上文字转白，保证对比度 */
.cm-cell.is-strong b,
.cm-cell.is-strong .cm-name {
  color: #fff;
}

.cm-cell.is-strong .cm-share {
  color: rgba(255, 255, 255, 0.82);
}

.cm-metrics {
  display: flex;
  flex-direction: column;
  gap: 9px;
  flex: 1;
  min-width: 180px;
  max-width: 320px;
}

.cm-metric {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr) 52px;
  align-items: center;
  gap: 9px;
}

.cm-metric-label {
  font-size: 11px;
  color: var(--ink-3);
  white-space: nowrap;
}

.cm-track {
  height: 6px;
  border-radius: 999px;
  background: var(--track-bg);
  overflow: hidden;
}

.cm-track i {
  display: block;
  height: 100%;
  border-radius: 999px;
  transition: width 0.4s ease;
}

.cm-metric b {
  font-size: 11.5px;
  font-weight: 650;
  color: var(--ink);
  text-align: right;
  white-space: nowrap;
}

/* 明细区紧凑档：方块缩到 70px，指标条不变 */
.cm.is-compact .cm-cell {
  width: 70px;
  height: 70px;
}

.cm.is-compact .cm-cell b {
  font-size: 15px;
}

.cm.is-compact .cm-name {
  font-size: 10px;
}
</style>
