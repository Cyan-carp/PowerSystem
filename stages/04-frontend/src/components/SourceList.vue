<script setup lang="ts">
/**
 * 一轮回答的来源列表：知识库章节、公网检索页、平台数据。
 *
 * 三类来源的可信度不一样，所以不合并成一条链接列表：
 * 文档能读全文，网页只能外跳且需人工校对，平台数据只是编号与摘要。
 */
import Icon from './Icon.vue'
import { sourceKindLabel, sourceLink, sourceSubtitle } from '../lib/agent-sources'
import type { RAGSource } from '../lib/api'
import { stamp } from '../lib/format'

defineProps<{ sources: RAGSource[] }>()
const emit = defineEmits<{ source: [item: RAGSource] }>()

const linkOf = (item: RAGSource) => sourceLink(item.url)
</script>

<template>
  <div class="sources">
    <article v-for="item in sources" :key="item.id" class="source">
      <div class="source-head">
        <strong>{{ item.title || item.id }}</strong>
        <span class="source-kind">{{ sourceKindLabel(item.kind) }}</span>
      </div>
      <p v-if="item.chapter" class="source-sub">章节 {{ item.chapter }}</p>
      <p class="source-sub">{{ sourceSubtitle(item, stamp) }}</p>
      <p v-if="item.summary" class="source-text">{{ item.summary }}</p>
      <button v-if="item.kind === 'document'" class="source-link" @click="emit('source', item)">
        <Icon name="arrowUpRight" :size="12" />阅读章节
      </button>
      <a
        v-else-if="item.kind === 'web' && linkOf(item)"
        class="source-link"
        :href="linkOf(item)!"
        target="_blank"
        rel="noopener noreferrer"
      >
        <Icon name="arrowUpRight" :size="12" />打开网页
      </a>
      <p v-if="item.kind === 'web'" class="source-warn">公网资料未经本站核对，仅供排查参考。</p>
    </article>
    <p v-if="!sources.length" class="source-empty">本轮没有引用来源。</p>
  </div>
</template>

<style scoped>
.source {
  padding: 9px 0;
  border-bottom: 1px dashed var(--line-soft);
}

.source:last-of-type {
  border-bottom: 0;
}

.source-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.source-head strong {
  font-size: 12.5px;
  font-weight: 550;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.source-kind {
  font-size: 10.5px;
  color: var(--brand-deep);
  background: var(--brand-soft);
  border-radius: var(--r-pill);
  padding: 1px 7px;
  white-space: nowrap;
}

.source-sub {
  margin-top: 3px;
  font-size: 11px;
  color: var(--ink-4);
  overflow-wrap: anywhere;
}

.source-text {
  margin-top: 5px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ink-3);
  overflow-wrap: anywhere;
}

.source-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 6px;
  font-size: 11.5px;
  font-weight: 550;
  color: var(--brand);
}

.source-link:hover {
  color: var(--brand-deep);
  text-decoration: underline;
}

.source-warn {
  margin-top: 4px;
  font-size: 10.5px;
  color: var(--warn-deep);
}

.source-empty {
  font-size: 12px;
  color: var(--ink-3);
}
</style>
