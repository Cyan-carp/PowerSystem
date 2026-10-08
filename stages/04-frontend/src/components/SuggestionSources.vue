<script setup lang="ts">
import { computed } from 'vue'
import type { AgentClaim, AgentEvidence, ChatEvidence } from '../types'
import { claimEvidence, evidenceRoute, operationNotice, publicLink, requiresEquipmentReview } from '../lib/agent-chat'
import { dateTime } from '../lib/format'

const props = defineProps<{ claim: AgentClaim; evidence: (AgentEvidence | ChatEvidence)[] }>()
const emit = defineEmits<{ 'read-source': [evidence: ChatEvidence] }>()
const sources = computed(() => claimEvidence(props.claim, props.evidence))
</script>

<template>
  <section class="suggestion">
    <p v-if="requiresEquipmentReview(claim)" role="note" class="operation-warning"><strong>{{ operationNotice }}</strong></p>
    <p class="suggestion-text">{{ claim.text }}</p>
    <details class="suggestion-sources">
      <summary>查看本条建议来源 · {{ claim.evidence_ids.join('、') }}</summary>
      <p v-if="sources.length !== claim.evidence_ids.length" class="source-warning">部分引用不可用，请重新取证后复核。</p>
      <article v-for="item in sources" :key="item.id" class="suggestion-source">
        <strong>{{ item.id }} · {{ item.kind === 'document' ? '知识库文档' : item.kind === 'web' ? '网上检索网页' : '事件数据快照' }} · {{ item.status }}</strong>
        <template v-if="item.kind === 'document'">
          <p>文档：{{ item.data.document }} · 章节：{{ item.chapter || item.data.heading }}</p>
          <p>版本：{{ item.document_version }}</p>
          <p class="suggestion-text">{{ item.data.content }}</p>
          <el-button @click="emit('read-source', item)">阅读引用章节</el-button>
        </template>
        <template v-else-if="item.kind === 'web'">
          <p>网页：{{ item.data.title }}</p>
          <p>具体来源：{{ item.url || item.source }}</p>
          <p class="suggestion-text">{{ item.data.summary }}</p>
          <p v-if="typeof item.data.published_at === 'string' && item.data.published_at">发布时间：{{ dateTime(item.data.published_at) }}</p>
          <a v-if="publicLink(item.url || item.source)" :href="publicLink(item.url || item.source)!" target="_blank" rel="noopener noreferrer">打开具体来源网页</a>
          <p class="source-warning">公开资料待专业工程师校对，尚未核实实际机型适用性。</p>
        </template>
        <template v-else>
          <p>事件来源：{{ item.source }}</p>
          <p class="source-warning">这是事件数据依据，不是知识库文档或设备操作手册。</p>
          <pre>{{ JSON.stringify(item.data, null, 2) }}</pre>
          <router-link v-if="evidenceRoute(item)" :to="evidenceRoute(item)!">查看业务页面</router-link>
        </template>
        <p>取证／检索时间：{{ dateTime(item.collected_at) }}<template v-if="item.data_time"> · 数据时间：{{ dateTime(item.data_time) }}</template></p>
      </article>
    </details>
  </section>
</template>

<style scoped>
.suggestion { border-top: 1px solid var(--line-soft); padding: 14px 0; }
.operation-warning {
  border: 1px solid var(--warning-line); border-left: 3px solid var(--warning-dot);
  background: var(--warning-soft); color: var(--warning);
  padding: 10px 12px; font-size: 13px; line-height: 1.7; margin: 0 0 10px;
}
.suggestion-text { white-space: pre-wrap; line-height: 1.8; overflow-wrap: anywhere; margin: 0 0 6px; color: var(--ink); font-size: 14px; }
summary { cursor: pointer; color: var(--brand); font-size: 12px; padding: 4px 0; list-style: none; display: flex; align-items: center; gap: 8px; }
summary::before { content: ""; width: 6px; height: 6px; border-right: 1.5px solid var(--brand); border-bottom: 1.5px solid var(--brand); transform: rotate(-45deg); }
details[open] summary::before { transform: rotate(45deg); }
summary:hover { color: var(--brand-deep); }
.suggestion-source { margin-top: 12px; padding: 14px; border: 1px solid var(--line); background: #fff; font-size: 13px; overflow-wrap: anywhere; }
.suggestion-source strong { color: var(--ink); font-size: 12px; }
.suggestion-source p { margin: 8px 0 0; color: var(--ink-2); }
.source-warning { color: var(--warning); font-size: 12px; }
a { color: var(--brand); }
a:hover { color: var(--brand-deep); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 280px; overflow: auto; background: var(--hover); border: 1px solid var(--line-soft); padding: 12px; font-size: 12px; margin: 10px 0 0; }
</style>
