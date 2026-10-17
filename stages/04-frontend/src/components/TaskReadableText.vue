<script setup lang="ts">
import { computed } from 'vue'
import { renderAnswer } from '../lib/rag-chat'
import { readableTaskText } from '../lib/task-presentation'
const props = defineProps<{ text: unknown }>()
const html = computed(() => renderAnswer(readableTaskText(props.text)))
</script>
<template><div class="task-prose" v-html="html" /></template>
<style scoped>
.task-prose{font-size:14px;line-height:1.85;color:var(--ink-2);overflow-wrap:anywhere;max-width:84ch}
.task-prose :deep(p){margin:0 0 12px}.task-prose :deep(p:last-child){margin-bottom:0}
.task-prose :deep(h1),.task-prose :deep(h2),.task-prose :deep(h3){font-size:16px;color:var(--ink);margin:22px 0 10px;line-height:1.5}
.task-prose :deep(ul),.task-prose :deep(ol){padding-left:24px;margin:12px 0}.task-prose :deep(li){padding:3px 0}
.task-prose :deep(strong){color:var(--ink)}.task-prose :deep(code){font-size:12px;background:var(--bg);padding:2px 4px}
.task-prose :deep(pre){white-space:pre-wrap;background:var(--bg);padding:12px;max-height:320px;overflow:auto}
.task-prose :deep(table){display:block;max-width:100%;overflow:auto;border-collapse:collapse}.task-prose :deep(th),.task-prose :deep(td){border:1px solid var(--line);padding:8px;text-align:left}
.task-prose :deep(a){color:var(--brand)}.task-prose :deep(blockquote){margin:12px 0;padding-left:12px;border-left:2px solid var(--brand-line)}
</style>
