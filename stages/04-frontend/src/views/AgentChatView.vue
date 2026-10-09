<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api'
import { chatStatusLabels, evidenceRoute, publicLink } from '../lib/agent-chat'
import { dateTime } from '../lib/format'
import type { AgentChatResponse, ChatEvidence, KnowledgeSource } from '../types'
import SuggestionSources from '../components/SuggestionSources.vue'

const route = useRoute()
const message = ref('')
const sessionID = ref<string>()
const turns = ref<{ question: string; response?: AgentChatResponse; error?: string }[]>([])
const pending = ref(false)
const source = ref<KnowledgeSource | null>(null)
const sourceError = ref('')
const sourceVersion = ref('')
const examples = ['设备状态如何？', '确认告警和已恢复有什么区别？', '设备 1 的预测是否新鲜？']
let controller: AbortController | null = null
let sourceController: AbortController | null = null
let generation = 0
watch(() => route.query.question, (value) => { if (typeof value === 'string') message.value = value.slice(0, 4000) }, { immediate: true })

function cancel(): void { controller?.abort(); generation++; pending.value = false; controller = null }
function reset(): void { cancel(); sourceController?.abort(); turns.value = []; sessionID.value = undefined; source.value = null; sourceError.value = '' }
function useExample(text: string): void { message.value = text }
async function send(): Promise<void> {
  if (pending.value || !message.value.trim()) return
  const question = message.value.trim()
  const index = turns.value.push({ question }) - 1
  message.value = ''
  pending.value = true
  const mine = ++generation
  controller = new AbortController()
  try {
    const response = await api.chat(question, sessionID.value, controller.signal)
    if (mine !== generation) return
    sessionID.value = response.session_id
    turns.value[index]!.response = response
  } catch (error) {
    if (mine === generation) turns.value[index]!.error = error instanceof Error ? error.message : '服务不可用'
  } finally { if (mine === generation) { pending.value = false; controller = null } }
}
async function showSource(evidence: ChatEvidence): Promise<void> {
  const id = evidence.source.match(/^\/api\/v1\/agent\/knowledge\/(K[a-f0-9]{20})$/)?.[1]
  if (!id) return
  sourceController?.abort()
  const current = new AbortController()
  sourceController = current
  sourceError.value = ''
  sourceVersion.value = evidence.document_version || ''
  try { const result = await api.knowledge(id, current.signal); if (!current.signal.aborted) source.value = result }
  catch (error) { if (!current.signal.aborted) sourceError.value = error instanceof Error ? error.message : '来源不可用' }
}
onUnmounted(() => { cancel(); sourceController?.abort() })
</script>

<template>
  <div class="chat-page">
    <section class="page-head">
      <div><span class="section-kicker">AGENT ASSISTANT</span><h1>值班问答</h1><p>受控知识库 · 只读业务查询 · 证据核对</p></div>
      <el-button v-if="turns.length" @click="reset">新会话</el-button>
    </section>
    <div class="chat-boundary">合成演示系统。建议须人工核查；请勿输入账号、密码、密钥或个人信息。</div>

    <section v-if="!turns.length" class="chat-empty surface">
      <div class="empty-box tall">
        <h2>描述你要核查的问题</h2>
        <p>例如：设备状态如何？确认告警和已恢复有什么区别？</p>
        <p>优先查本地资料和业务数据；适合的通用问题可联网补充。没有可核验来源时仅显示明确标注的假设。</p>
        <div class="chat-examples"><button v-for="item in examples" :key="item" type="button" class="chat-example" @click="useExample(item)">{{ item }}</button></div>
      </div>
    </section>

    <article v-for="(turn, index) in turns" :key="index" class="chat-turn surface">
      <header class="chat-turn-head">
        <span class="chat-q-label">问题 {{ index + 1 }}</span>
        <span v-if="turn.response" class="chat-status">{{ chatStatusLabels[turn.response.status] }}</span>
        <span v-if="turn.response?.conclusion" class="chat-status">可核验来源覆盖率 {{ turn.response.source_coverage_percent }}%（{{ turn.response.sourced_claims }}/{{ turn.response.total_claims }} 句陈述附来源；不代表正确率）</span>
      </header>
      <p class="chat-question">{{ turn.question }}</p>
      <p v-if="turn.error" class="chat-error" role="alert">{{ turn.error }}</p>
      <template v-if="turn.response">
        <p v-for="notice in turn.response.notices" :key="notice" class="chat-notice">{{ notice }}</p>
        <section class="chat-answer">
          <h3 class="chat-block-title">结论</h3>
          <p class="chat-text chat-conclusion">{{ turn.response.conclusion?.text || '无法判断：未取得足够有效依据。' }} <small v-if="turn.response?.answer_mode === 'hypothesis'" class="chat-evidence-ids">未核验假设</small><small v-if="turn.response.conclusion?.evidence_ids.length" class="chat-evidence-ids">{{ turn.response.conclusion?.evidence_ids.join('、') }}</small></p>
        </section>
        <template v-if="turn.response.suggestions.length">
          <h3 class="chat-block-title">人工核查建议</h3>
          <SuggestionSources v-for="(suggestion, suggestionIndex) in turn.response.suggestions" :key="suggestionIndex" :claim="suggestion" :evidence="turn.response.evidence" @read-source="showSource" />
        </template>
        <h3 v-if="turn.response.evidence.length" class="chat-block-title">取证与来源</h3>
        <details v-for="item in turn.response.evidence" :key="item.id" class="chat-evidence">
          <summary>{{ item.id }} · {{ item.kind === 'document' ? (item.data.source_kind === 'manual_summary' ? '型号手册整理' : '项目文档') : item.kind === 'web' ? '联网来源' : '业务数据' }} · {{ item.chapter || item.tool }} · {{ item.status }}</summary>
          <div class="chat-evidence-body">
            <p>查询时间：{{ dateTime(item.collected_at) }}<template v-if="item.data_time"> · 数据时间：{{ dateTime(item.data_time) }}</template></p>
            <p>来源：{{ item.source }}</p>
            <p v-if="item.kind === 'web' && typeof item.data.published_at === 'string' && item.data.published_at">发布时间：{{ dateTime(item.data.published_at) }}</p>
            <p v-if="item.document_version">知识索引版本：{{ item.document_version }}</p>
            <p v-if="item.kind === 'document' && item.data.source_revision">原始来源版本：{{ item.data.source_revision }}</p>
            <div class="chat-evidence-actions">
              <el-button v-if="item.kind === 'document'" size="small" @click="showSource(item)">阅读引用章节</el-button>
              <a v-if="item.kind === 'web' && publicLink(item.url)" :href="publicLink(item.url)!" target="_blank" rel="noopener noreferrer">打开外部来源</a>
              <router-link v-if="evidenceRoute(item)" :to="evidenceRoute(item)!">查看业务页面</router-link>
            </div>
            <p v-if="item.kind === 'document'" class="chat-text">{{ String(item.data.content || '').slice(0, 300) }}…</p>
            <p v-else-if="item.kind === 'web'" class="chat-text">{{ item.data.title }}：{{ item.data.summary }}</p>
            <pre v-else>{{ JSON.stringify(item.data, null, 2) }}</pre>
          </div>
        </details>
        <p v-for="limit in turn.response.limitations" :key="limit" class="chat-limit">{{ limit }}</p>
        <footer class="chat-turn-foot">模型：{{ turn.response.model }} · 请求：{{ turn.response.request_id }}</footer>
      </template>
      <p v-else-if="!turn.error" class="chat-waiting">{{ pending && index === turns.length - 1 ? '正在检索与取证…' : '等待已取消，本轮未展示回答。' }}</p>
    </article>

    <p v-if="sourceError" class="chat-error" role="alert">{{ sourceError }}</p>
    <section v-if="source" class="chat-source surface">
      <header class="chat-source-head"><strong>{{ source.heading }}</strong><el-button size="small" @click="source = null">关闭章节</el-button></header>
      <small class="chat-source-meta">{{ source.document }} · {{ source.source_kind === 'manual_summary' ? '型号手册整理' : source.authority === 'historical' ? '历史记录' : '项目文档' }} {{ source.source_revision || source.document_date }}</small>
      <p v-if="sourceVersion && source.version !== sourceVersion" class="chat-notice">知识版本已变化，以下为当前章节。请重新提问取得当前版本的回答与证据。</p>
      <pre>{{ source.content }}</pre>
    </section>

    <form class="chat-compose surface" @submit.prevent="send">
      <el-input v-model="message" type="textarea" :rows="3" maxlength="4000" show-word-limit placeholder="输入问题（不自动发送页面帮助带入的问题）" aria-label="值班问题" />
      <div class="chat-compose-actions">
        <el-button v-if="pending" @click="cancel">取消等待</el-button>
        <el-button native-type="submit" type="primary" :disabled="pending || !message.trim()">{{ pending ? '处理中' : '发送问题' }}</el-button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.chat-page { max-width: 1100px; margin: 0 auto; }

/* 边界提醒 */
.chat-boundary {
  border: 1px solid var(--warning-line); border-left: 3px solid var(--warning-dot);
  background: var(--warning-soft); color: var(--warning);
  padding: 10px 12px; font-size: 13px;
}

/* 空态 */
.chat-empty .empty-box h2 { font-size: 16px; font-weight: 600; color: var(--ink); margin: 0 0 8px; }
.chat-empty .empty-box p { margin: 0 0 6px; font-size: 13px; }
.chat-examples { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
.chat-example {
  border: 1px solid var(--line-strong); background: #fff; color: var(--ink-2);
  padding: 8px 16px; font-size: 13px; border-radius: 999px;
}
.chat-example:hover { border-color: var(--brand); color: var(--brand); background: var(--brand-soft); }

/* 问答回合 */
.chat-turn { margin-top: 16px; }
.chat-turn-head {
  display: flex; justify-content: space-between; align-items: center; gap: 16px;
  padding-bottom: 12px; border-bottom: 1px solid var(--line-soft);
}
.chat-q-label { font-size: 12px; font-weight: 700; letter-spacing: 1px; color: var(--ink-2); }
.chat-status { font-size: 12px; color: var(--brand); font-weight: 600; }
.chat-question {
  background: var(--brand-soft); color: var(--ink); border: 1px solid var(--brand-line);
  border-radius: 12px;
  padding: 12px 16px; margin: 14px 0 0;
  font-size: 14px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere;
}
.chat-error { color: var(--danger); font-size: 13px; margin: 12px 0 0; }
.chat-notice {
  border-left: 3px solid var(--warning-dot); background: var(--warning-soft);
  color: var(--warning); padding: 10px 12px; margin: 12px 0 0; font-size: 12px;
}
.chat-waiting { color: var(--ink-3); font-size: 13px; margin: 14px 0 0; }

/* 回答区 */
.chat-answer { margin-top: 14px; }
.chat-block-title {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--ink); margin: 18px 0 8px;
}
.chat-block-title::before { content: ""; width: 6px; height: 6px; background: var(--brand); }
.chat-conclusion {
  border: 1px solid var(--line); border-left: 3px solid var(--brand);
  background: #fff; padding: 12px 14px; margin: 0; font-size: 14px;
}
.chat-evidence-ids { display: block; color: var(--ink-3); font-size: 11px; margin-top: 6px; }
.chat-text { white-space: pre-wrap; line-height: 1.8; overflow-wrap: anywhere; }

/* 证据展开 */
.chat-evidence { border-top: 1px solid var(--line-soft); font-size: 13px; }
.chat-evidence summary {
  cursor: pointer; padding: 11px 4px; color: var(--ink-2); font-size: 12px;
  list-style: none; display: flex; align-items: center; gap: 8px;
}
.chat-evidence summary::before { content: ""; width: 6px; height: 6px; border-right: 1.5px solid var(--ink-3); border-bottom: 1.5px solid var(--ink-3); transform: rotate(-45deg); transition: none; }
.chat-evidence[open] summary::before { transform: rotate(45deg); }
.chat-evidence summary:hover { color: var(--ink); background: var(--hover); }
.chat-evidence-body { padding: 4px 4px 14px 18px; color: var(--ink-2); }
.chat-evidence-body p { margin: 0 0 8px; overflow-wrap: anywhere; }
.chat-evidence-actions { display: flex; align-items: center; gap: 14px; margin: 4px 0 10px; }
.chat-evidence-actions a, .chat-evidence-body a { color: var(--brand); font-size: 13px; }
.chat-evidence-actions a:hover, .chat-evidence-body a:hover { color: var(--brand-deep); }

/* 局限与回合脚 */
.chat-limit { color: var(--ink-3); font-size: 12px; margin: 10px 0 0; }
.chat-turn-foot {
  margin-top: 16px; padding-top: 10px; border-top: 1px solid var(--line-soft);
  color: var(--ink-3); font-size: 11px;
}

/* 来源阅读 */
.chat-source { margin-top: 16px; padding: 16px 20px; }
.chat-source-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding-bottom: 10px; border-bottom: 1px solid var(--line-soft); }
.chat-source-head strong { font-size: 15px; color: var(--ink); }
.chat-source-meta { display: block; color: var(--ink-3); font-size: 12px; margin: 10px 0; }

/* 输入区 */
.chat-compose { margin-top: 16px; padding: 16px 20px; border-radius: 16px; }
.chat-compose :deep(.el-textarea__inner) { border-radius: 10px; }
.chat-compose-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 12px; }

pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 350px; overflow: auto; font-size: 12px; background: var(--hover); border: 1px solid var(--line-soft); padding: 12px; margin: 0 0 10px; }
</style>
