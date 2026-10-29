<script setup lang="ts">
/**
 * 卡片右上角的「···」菜单。
 * 参考稿里它只是装饰，这里挂上真实动作：导出该卡数据 JSON，以及跳到对应页面。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import Icon from './Icon.vue'

const props = withDefaults(
  defineProps<{
    payload: unknown
    filename: string
    to?: string
    toLabel?: string
  }>(),
  { to: '', toLabel: '查看全部' },
)

const open = ref(false)
const done = ref(false)
const root = ref<HTMLElement | null>(null)
let timer = 0

function onDoc(event: MouseEvent) {
  if (root.value && !root.value.contains(event.target as Node)) open.value = false
}
function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') open.value = false
}
onMounted(() => {
  document.addEventListener('click', onDoc)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDoc)
  document.removeEventListener('keydown', onKey)
  window.clearTimeout(timer)
})

function exportJson() {
  const blob = new Blob([JSON.stringify(props.payload ?? null, null, 2)], {
    type: 'application/json;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${props.filename}-${new Date().toISOString().slice(0, 10)}.json`
  anchor.click()
  URL.revokeObjectURL(url)
  open.value = false
  done.value = true
  window.clearTimeout(timer)
  timer = window.setTimeout(() => (done.value = false), 1600)
}
</script>

<template>
  <div ref="root" class="card-menu">
    <button
      class="card-menu-btn"
      :class="{ 'is-open': open }"
      :aria-expanded="open"
      aria-label="卡片操作"
      title="卡片操作"
      @click.stop="open = !open"
    >
      <Icon name="more" :size="17" :stroke="2.4" />
    </button>
    <!-- 卡片自己的动作（导出 CSV、重新载入等）从插槽进来，顺序在通用项之前 -->
    <div v-if="open" class="card-menu-pop" role="menu" @click="open = false">
      <slot />
      <RouterLink v-if="to" class="card-menu-item" :to="to" @click="open = false">
        {{ toLabel }}
      </RouterLink>
      <button class="card-menu-item" @click="exportJson">导出该卡数据 JSON</button>
    </div>
    <span v-if="done" class="card-menu-done">已导出</span>
  </div>
</template>

<style scoped>
.card-menu {
  position: relative;
  margin-left: auto;
  display: flex;
  align-items: center;
}

.card-menu-btn {
  width: 24px;
  height: 24px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  color: var(--ink-3);
  transition: background 0.15s ease, color 0.15s ease;
}

.card-menu-btn:hover,
.card-menu-btn.is-open {
  background: var(--card-soft);
  color: var(--ink);
}

.card-menu-btn:focus-visible {
  outline: 2px solid var(--brand-line);
  outline-offset: 1px;
}

.card-menu-pop {
  position: absolute;
  top: 28px;
  right: 0;
  z-index: 40;
  min-width: 152px;
  padding: 5px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 6px;
  box-shadow: var(--shadow-2);
}

.card-menu-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 7px 9px;
  border-radius: 7px;
  font-size: 12px;
  color: var(--ink-2);
  white-space: nowrap;
}

.card-menu-item:hover {
  background: var(--card-soft);
  color: var(--ink);
}

.card-menu-done {
  position: absolute;
  top: 28px;
  right: 0;
  font-size: 11px;
  color: var(--ok-deep);
  white-space: nowrap;
}
</style>
