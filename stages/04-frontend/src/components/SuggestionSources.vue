<script setup lang="ts">
import { computed } from 'vue'
import type { AgentClaim, AgentEvidence, ChatEvidence } from '../types'
import { claimEvidence, evidenceRoute, operationNotice, publicLink, requiresEquipmentReview } from '../lib/agent-chat'
import { dateTime } from '../lib/format'
import { evidenceSourceLabel, evidenceStatusLabel } from '../lib/interpretation-display'
import { interpretationParagraphs } from '../lib/interpretation-layout'

const props = defineProps<{ claim: AgentClaim; evidence: (AgentEvidence | ChatEvidence)[]; interpretationMode?: boolean; order?: number }>()
const emit = defineEmits<{ 'read-source': [evidence: ChatEvidence] }>()
const sources = computed(() => claimEvidence(props.claim, props.evidence))
const paragraphs = computed(() => interpretationParagraphs(props.claim.text.replace(/[（(]\s*需人工确认执行\s*[）)]\s*[。.]?\s*$/, '')))
</script>

<template>
  <section class="suggestion" :class="{ 'suggestion-interpretation': interpretationMode }">
    <header v-if="interpretationMode" class="suggestion-heading"><strong>检查建议 {{ order }}</strong><span>人工核查参考</span></header>
    <p v-if="!interpretationMode && requiresEquipmentReview(claim)" role="note" class="operation-warning"><strong>{{ operationNotice }}</strong></p>
    <div v-if="interpretationMode" class="suggestion-paragraphs"><p v-for="(paragraph, index) in paragraphs" :key="index" class="suggestion-text">{{ paragraph }}</p></div>
    <p v-else class="suggestion-text">{{ claim.text }}</p>
    <details class="suggestion-sources">
      <summary>查看本条建议来源 · {{ claim.evidence_ids.join('、') }}</summary>
      <p v-if="sources.length !== claim.evidence_ids.length" class="source-warning">部分引用不可用，请重新取证后复核。</p>
      <article v-for="item in sources" :key="item.id" class="suggestion-source">
        <strong>{{ item.id }} · {{ item.kind === 'document' ? '知识库文档' : item.kind === 'web' ? '网上检索网页' : '事件数据快照' }} · {{ interpretationMode ? evidenceStatusLabel(item.status) : item.status }}</strong>
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
          <p>事件来源：{{ interpretationMode ? evidenceSourceLabel(item) : item.source }}</p>
          <details v-if="interpretationMode" class="source-raw"><summary>查看原始来源路径</summary><code>{{ item.source }}</code></details>
          <p class="source-warning">这是事件数据依据，不是知识库文档或设备操作手册。</p>
          <details v-if="interpretationMode" class="source-raw"><summary>查看原始数据</summary><pre>{{ JSON.stringify(item.data, null, 2) }}</pre></details>
          <pre v-else>{{ JSON.stringify(item.data, null, 2) }}</pre>
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
.suggestion-source { margin-top: 12px; padding: 14px; border: 1px solid var(--line); background: var(--card-2); border-radius: var(--r-sm); font-size: 13px; overflow-wrap: anywhere; }
.suggestion-source strong { color: var(--ink); font-size: 12px; }
.suggestion-source p { margin: 8px 0 0; color: var(--ink-2); }
.source-warning { color: var(--warning); font-size: 12px; }
a { color: var(--brand); }
a:hover { color: var(--brand-deep); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 280px; overflow: auto; background: var(--hover); border: 1px solid var(--line-soft); padding: 12px; font-size: 12px; margin: 10px 0 0; }
.source-raw { margin-top: 8px; font-size: 12px; color: var(--ink-2); }
.source-raw code { display: block; margin-top: 8px; padding: 8px 10px; overflow-wrap: anywhere; background: var(--hover); border: 1px solid var(--line-soft); }
.suggestion-interpretation { border: 1px solid var(--brand-line); border-left: 3px solid var(--brand); border-radius: var(--r-md); padding: 16px 18px; margin: 12px 0; background: var(--brand-soft); }
.suggestion-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.suggestion-heading strong { font-size: 13px; color: var(--brand); }
.suggestion-heading span { font-size: 11px; color: var(--ink-3); }
.suggestion-interpretation .suggestion-text { font-size: 15px; line-height: 1.9; font-weight: 600; color: var(--ink); }
.suggestion-interpretation .suggestion-paragraphs { margin: 12px 0 14px; }
.suggestion-interpretation .suggestion-paragraphs p:last-child { margin-bottom: 0; }
.suggestion-interpretation .suggestion-sources > summary { font-size: 11px; color: var(--ink-2); }
.suggestion-interpretation .suggestion-source .suggestion-text { font-size: 12px; font-weight: 400; color: var(--ink-2); }
@media(max-width: 540px) { .suggestion-interpretation { padding: 14px; } .suggestion-interpretation .suggestion-text { font-size: 14px; } }
</style>
