<script setup lang="ts">
import { computed, ref } from 'vue'
import Icon from './Icon.vue'

/* 所有表格共用一条分页条：箭头 + 页码块 + 跳转输入。
   页码块保证「首末页始终可点、当前页两侧各留一页」，其余折叠成省略号，
   这样任意一页最多两次点击可达，不用一页页翻。 */

const props = withDefaults(
  defineProps<{
    page: number
    totalPages: number
    /** 总条数与每页条数同时给，才会显示「显示 1–8 / 143 台」 */
    total?: number
    pageSize?: number
    unit?: string
    /** 右侧补充说明，如「每页 8 台 · 覆盖率低于 90% 记为数据不足」 */
    note?: string
  }>(),
  { total: undefined, pageSize: undefined, unit: '条', note: '' },
)

const emit = defineEmits<{ (event: 'update:page', value: number): void }>()

const draft = ref('')

const clamp = (value: number) => Math.min(Math.max(1, value), props.totalPages)

const go = (value: number) => {
  const target = clamp(value)
  if (target !== props.page) emit('update:page', target)
}

const blocks = computed<(number | 'gap')[]>(() => {
  const last = Math.max(1, props.totalPages)
  if (last <= 7) return Array.from({ length: last }, (_, index) => index + 1)
  const keep = new Set<number>([1, last, props.page - 1, props.page, props.page + 1])
  if (props.page <= 3) [2, 3, 4].forEach((value) => keep.add(value))
  if (props.page >= last - 2) [last - 1, last - 2, last - 3].forEach((value) => keep.add(value))
  const sorted = [...keep].filter((value) => value >= 1 && value <= last).sort((a, b) => a - b)
  const out: (number | 'gap')[] = []
  sorted.forEach((value, index) => {
    if (index > 0 && value - sorted[index - 1] > 1) out.push('gap')
    out.push(value)
  })
  return out
})

const countText = computed(() => {
  if (props.total == null || props.pageSize == null) return ''
  const from = props.total ? (props.page - 1) * props.pageSize + 1 : 0
  const to = Math.min(props.page * props.pageSize, props.total)
  return `显示 ${from}–${to} / ${props.total} ${props.unit}`
})

const commit = () => {
  const value = Number(draft.value.replace(/[^\d]/g, ''))
  draft.value = ''
  if (Number.isFinite(value) && value >= 1) go(value)
}
</script>

<template>
  <div class="pager">
    <button class="pager-btn" :disabled="page <= 1" title="上一页" @click="go(page - 1)">
      <Icon name="chevronLeft" :size="15" />
    </button>

    <div class="pg-nums">
      <template v-for="(block, index) in blocks" :key="`${block}-${index}`">
        <span v-if="block === 'gap'" class="pg-gap">…</span>
        <button
          v-else
          class="pg-num num"
          :class="{ 'is-on': block === page }"
          :title="`第 ${block} 页`"
          @click="go(block)"
        >
          {{ block }}
        </button>
      </template>
    </div>

    <button class="pager-btn" :disabled="page >= totalPages" title="下一页" @click="go(page + 1)">
      <Icon name="chevronRight" :size="15" />
    </button>

    <div class="pg-tail">
      <span v-if="note" class="pg-note">{{ note }}</span>
      <span v-if="countText" class="pg-meta">{{ countText }}</span>
      <label class="pg-jump">
        跳至
        <input
          v-model="draft"
          inputmode="numeric"
          maxlength="4"
          aria-label="跳转到指定页"
          @keydown.enter.prevent="commit"
          @blur="commit"
        />
        页
      </label>
    </div>
  </div>
</template>

<style scoped>
.pager {
  flex-wrap: wrap;
  row-gap: 8px;
}

.pg-nums {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}

.pg-nums::-webkit-scrollbar {
  display: none;
}

.pg-num {
  flex: 0 0 auto;
  min-width: 26px;
  height: 26px;
  padding: 0 7px;
  border-radius: 4px;
  border: 1px solid transparent;
  color: var(--ink-3);
  font-size: 12px;
  font-weight: 600;
}

.pg-num:hover {
  background: var(--card-soft);
  border-color: var(--line);
  color: var(--ink);
}

.pg-num.is-on {
  background: var(--brand-soft);
  border-color: transparent;
  color: var(--brand-deep);
}

.pg-gap {
  flex: 0 0 auto;
  padding: 0 2px;
  color: var(--ink-4);
  letter-spacing: 0.1em;
}

.pg-tail {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 14px;
}

.pg-note,
.pg-meta {
  font-size: 11.5px;
  color: var(--ink-3);
}

.pg-jump {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--ink-3);
  white-space: nowrap;
}

.pg-jump input {
  width: 42px;
  height: 26px;
  padding: 0 6px;
  border-radius: 4px;
  border: 1px solid var(--line-strong);
  background: #fff;
  color: var(--ink);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  text-align: center;
}

.pg-jump input::placeholder {
  font-weight: 400;
  color: var(--ink-4);
}

.pg-jump input:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}
</style>
