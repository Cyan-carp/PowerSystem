<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    value: number
    label: string
    note?: string
    tone?: 'brand' | 'ok' | 'warn' | 'bad' | 'violet' | 'info'
    size?: number
  }>(),
  { tone: 'brand', size: 96, note: '' },
)

const thickness = 11
const radius = computed(() => (props.size - thickness) / 2)
const circumference = computed(() => 2 * Math.PI * radius.value)
const clamped = computed(() => Math.min(100, Math.max(0, Number.isFinite(props.value) ? props.value : 0)))
const dash = computed(() => `${(clamped.value / 100) * circumference.value} ${circumference.value}`)
const shown = computed(() => `${Math.round(clamped.value)}%`)
const palette: Record<string, string> = {
  brand: '#2563eb',
  ok: '#10b981',
  warn: '#f59e0b',
  bad: '#ef4444',
  violet: '#8b5cf6',
  info: '#2563eb',
}
const stroke = computed(() => palette[props.tone] || palette.brand)
</script>

<template>
  <div class="donut" :style="{ width: `${size}px` }">
    <div class="ring" :style="{ width: `${size}px`, height: `${size}px` }">
      <svg :width="size" :height="size" :viewBox="`0 0 ${size} ${size}`">
        <circle
          class="track"
          :cx="size / 2"
          :cy="size / 2"
          :r="radius"
          fill="none"
          :stroke-width="thickness"
        />
        <circle
          class="value"
          :cx="size / 2"
          :cy="size / 2"
          :r="radius"
          fill="none"
          :stroke="stroke"
          :stroke-width="thickness"
          stroke-linecap="round"
          :stroke-dasharray="dash"
          :transform="`rotate(-90 ${size / 2} ${size / 2})`"
        />
      </svg>
      <span class="center num">{{ shown }}</span>
    </div>
    <span class="label">{{ label }}</span>
    <span v-if="note" class="note">{{ note }}</span>
  </div>
</template>

<style scoped>
.donut {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.ring {
  position: relative;
  display: grid;
  place-items: center;
}

.ring svg {
  display: block;
}

.track {
  stroke: #eef2f8;
}

.value {
  transition: stroke-dasharray 0.6s cubic-bezier(0.22, 0.8, 0.32, 1);
}

.center {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
}

.label {
  margin-top: 7px;
  font-size: 12px;
  color: var(--ink-2);
  text-align: center;
}

.note {
  font-size: 10.5px;
  color: var(--ink-4);
  text-align: center;
  font-variant-numeric: tabular-nums;
}
</style>
