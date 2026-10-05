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
let controller: AbortController | null = null
let sourceController: AbortController | null = null
let generation = 0
watch(() => route.query.question, (value) => { if (typeof value === 'string') message.value = value.slice(0, 4000) }, { immediate: true })

function cancel(): void { controller?.abort(); generation++; pending.value = false; controller = null }
function reset(): void { cancel(); sourceController?.abort(); turns.value = []; sessionID.value = undefined; source.value = null; sourceError.value = '' }
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
    <div class="page-heading"><div><h1>值班问答</h1><p>受控知识库 · 只读业务查询 · 证据核对</p></div><el-button @click="reset">新会话</el-button></div>
    <div class="chat-boundary">合成演示系统。建议须人工核查；请勿输入账号、密码、密钥或个人信息。</div>
    <section v-if="!turns.length" class="chat-empty"><h2>描述你要核查的问题</h2><p>例如：确认告警和已恢复有什么区别？设备 1 的预测是否新鲜？</p><p>本地知识库不足时会显示提示，适合的通用问题再联网补充。</p></section>
    <article v-for="(turn, index) in turns" :key="index" class="chat-turn">
      <header><strong>问题 {{ index + 1 }}</strong><span v-if="turn.response">{{ chatStatusLabels[turn.response.status] }}</span></header>
      <p class="chat-text">{{ turn.question }}</p>
      <p v-if="turn.error" role="alert">{{ turn.error }}</p>
      <template v-if="turn.response">
        <p v-for="notice in turn.response.notices" :key="notice" class="chat-notice">{{ notice }}</p>
        <h3>结论</h3><p class="chat-text">{{ turn.response.conclusion?.text || '无法判断：未取得足够有效依据。' }} <small>{{ turn.response.conclusion?.evidence_ids.join('、') }}</small></p>
        <h3 v-if="turn.response.suggestions.length">人工核查建议</h3>
        <SuggestionSources v-for="(suggestion, suggestionIndex) in turn.response.suggestions" :key="suggestionIndex" :claim="suggestion" :evidence="turn.response.evidence" @read-source="showSource" />
        <details v-for="item in turn.response.evidence" :key="item.id" class="chat-evidence"><summary>{{ item.id }} · {{ item.kind === 'document' ? '项目文档' : item.kind === 'web' ? '联网来源' : '业务数据' }} · {{ item.chapter || item.tool }} · {{ item.status }}</summary>
          <p>查询时间：{{ dateTime(item.collected_at) }}<template v-if="item.data_time"> · 数据时间：{{ dateTime(item.data_time) }}</template></p>
          <p>来源：{{ item.source }}</p>
          <p v-if="item.kind === 'web' && typeof item.data.published_at === 'string' && item.data.published_at">发布时间：{{ dateTime(item.data.published_at) }}</p>
          <p v-if="item.document_version">文档版本：{{ item.document_version }}</p>
          <el-button v-if="item.kind === 'document'" @click="showSource(item)">阅读引用章节</el-button>
          <a v-if="item.kind === 'web' && publicLink(item.url)" :href="publicLink(item.url)!" target="_blank" rel="noopener noreferrer">打开外部来源</a>
          <router-link v-if="evidenceRoute(item)" :to="evidenceRoute(item)!">查看业务页面</router-link>
          <p v-if="item.kind === 'document'" class="chat-text">{{ String(item.data.content || '').slice(0, 300) }}…</p>
          <p v-else-if="item.kind === 'web'" class="chat-text">{{ item.data.title }}：{{ item.data.summary }}</p>
          <pre v-else>{{ JSON.stringify(item.data, null, 2) }}</pre>
        </details>
        <p v-for="limit in turn.response.limitations" :key="limit" class="chat-limit">{{ limit }}</p>
        <small>模型：{{ turn.response.model }} · 请求：{{ turn.response.request_id }}</small>
      </template>
      <p v-else-if="!turn.error">{{ pending && index === turns.length - 1 ? '正在检索与取证…' : '等待已取消，本轮未展示回答。' }}</p>
    </article>
    <p v-if="sourceError" role="alert">{{ sourceError }}</p>
    <section v-if="source" class="chat-source"><header><strong>{{ source.heading }}</strong><el-button @click="source = null">关闭章节</el-button></header><small>{{ source.document }} · {{ source.authority === 'historical' ? '历史记录' : '当前文档' }} {{ source.document_date }}</small><p v-if="sourceVersion && source.version !== sourceVersion" class="chat-notice">知识版本已变化，以下为当前章节。请重新提问取得当前版本的回答与证据。</p><pre>{{ source.content }}</pre></section>
    <form class="chat-compose" @submit.prevent="send"><el-input v-model="message" type="textarea" :rows="3" maxlength="4000" show-word-limit placeholder="输入问题（不自动发送页面帮助带入的问题）" aria-label="值班问题" /><div><el-button v-if="pending" @click="cancel">取消等待</el-button><el-button native-type="submit" type="primary" :disabled="pending || !message.trim()">{{ pending ? '处理中' : '发送问题' }}</el-button></div></form>
  </div>
</template>

<style scoped>
.chat-page { max-width: 1100px; margin: 0 auto; }
.page-heading, .chat-turn header, .chat-source header { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
h1 { font-size: 22px; margin: 0; } h2, h3 { font-size: 15px; } .page-heading p, small, .chat-limit { color: #627581; font-size: 12px; }
.chat-boundary, .chat-notice { border-left: 3px solid #b8872a; background: #fff; padding: 12px; font-size: 13px; }
.chat-empty, .chat-turn, .chat-source, .chat-compose { background: white; border: 1px solid #dce2e6; padding: 20px; margin-top: 16px; }
.chat-turn header { border-bottom: 1px solid #dce2e6; padding-bottom: 12px; font-size: 13px; }
.chat-text { white-space: pre-wrap; line-height: 1.8; overflow-wrap: anywhere; }
.chat-evidence p, small { overflow-wrap: anywhere; }
.chat-evidence { border-top: 1px solid #dce2e6; padding: 12px 0; font-size: 13px; } summary { cursor: pointer; } a { margin-right: 16px; color: #087f78; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 350px; overflow: auto; font-size: 12px; background: #f7f9fa; padding: 12px; }
.chat-compose > div:last-child { display: flex; justify-content: flex-end; gap: 10px; margin-top: 12px; }
</style>
