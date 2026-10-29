<script setup lang="ts">
import { computed } from 'vue'
import { readableTaskText } from '../lib/task-presentation'
import { renderAnswer } from '../lib/task-labels'

const props = defineProps<{ text: unknown }>()
const html = computed(() => renderAnswer(readableTaskText(props.text)))
</script>

<template>
  <div class="task-prose" v-html="html" />
</template>

<style scoped>
.task-prose {
  font-size: 13px;
  line-height: 1.8;
  color: var(--ink-2);
  overflow-wrap: anywhere;
  max-width: 84ch;
}

.task-prose :deep(p) {
  margin: 0 0 10px;
}

.task-prose :deep(p:last-child) {
  margin-bottom: 0;
}

.task-prose :deep(h1),
.task-prose :deep(h2),
.task-prose :deep(h3),
.task-prose :deep(h4) {
  font-size: 14px;
  color: var(--ink);
  margin: 18px 0 8px;
  line-height: 1.5;
}

.task-prose :deep(ul),
.task-prose :deep(ol) {
  padding-left: 22px;
  margin: 10px 0;
}

.task-prose :deep(li) {
  padding: 3px 0;
}

.task-prose :deep(strong) {
  color: var(--ink);
}

.task-prose :deep(code) {
  font-size: 11.5px;
  background: var(--card-soft);
  padding: 2px 4px;
  border-radius: var(--r-sm);
}

.task-prose :deep(pre) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--card-soft);
  padding: 10px;
  border-radius: var(--r-sm);
  max-height: 320px;
  overflow: auto;
}

.task-prose :deep(table) {
  display: block;
  max-width: 100%;
  overflow: auto;
  border-collapse: collapse;
}

.task-prose :deep(th),
.task-prose :deep(td) {
  border: 1px solid var(--line);
  padding: 7px 9px;
  text-align: left;
}

.task-prose :deep(a) {
  color: var(--brand);
}

.task-prose :deep(blockquote) {
  margin: 10px 0;
  padding-left: 12px;
  border-left: 2px solid var(--brand-line);
  color: var(--ink-3);
}
</style>
