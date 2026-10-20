<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, ArrowRight, Delete, Promotion } from '@element-plus/icons-vue'
import { api } from '../lib/api'
import { renderAnswer, sourceLink, programReplyLabel, canVerify } from '../lib/rag-chat'
import { hasBusinessEvidence } from '../lib/task-presentation'
import { dateTime, sessionRecency } from '../lib/format'
import type { Device, RAGSession, RAGTurn, VerificationCheck } from '../types'
import BusinessQuality from '../components/BusinessQuality.vue'
import BusinessEvidence from '../components/BusinessEvidence.vue'
import VerificationReport from '../components/VerificationReport.vue'
import OpsTaskCard from '../components/OpsTaskCard.vue'
import TaskRecordsPanel from '../components/TaskRecordsPanel.vue'
import { activeTaskStatuses } from '../lib/ops-task'
import type { OpsTask } from '../types'
const recoveredTasks = ref<OpsTask[]>([])
const sessionTasksMore = ref(false)
const sessionTasksLoading = ref(false)
const sessionTasksError = ref('')
const tasksOpen = ref(false)
const taskRecordFilter = ref<'all' | 'unlinked' | 'active'>('all')
const hasActiveTasks = ref(false)
const noticeError = ref('')
let taskListController: AbortController | undefined, noticeController: AbortController | undefined
let taskListVersion = 0, sessionTasksOffset = 0
let noticeTimer: ReturnType<typeof setTimeout> | undefined
let noticeLoading = false, disposed = false

function clearSessionTasks(): void {
  taskListVersion++; taskListController?.abort()
  recoveredTasks.value = []; sessionTasksMore.value = false; sessionTasksError.value = ''
  sessionTasksLoading.value = false; sessionTasksOffset = 0
}
async function refreshTasks(sid?: string, append = false): Promise<void> {
  if (!sid) { clearSessionTasks(); return }
  if (sid !== sessionID.value) return
  taskListController?.abort()
  const request = new AbortController(); taskListController = request
  const version = ++taskListVersion, mine = generation
  if (!append) { recoveredTasks.value = []; sessionTasksOffset = 0; sessionTasksMore.value = false }
  sessionTasksLoading.value = true; sessionTasksError.value = ''
  try {
    const data = await api.opsTasks(sid, sessionTasksOffset, { signal: request.signal })
    if (version !== taskListVersion || mine !== generation || sessionID.value !== sid || disposed) return
    const represented = new Set(turns.value.flatMap(turn => (turn.response?.task_cards || []).map(card => card.task_id)))
    const unique = new Map(recoveredTasks.value.map(task => [task.task_id, task]))
    for (const task of data.list) if (task.session_id === sid && !represented.has(task.task_id)) unique.set(task.task_id, task)
    recoveredTasks.value = [...unique.values()]
    sessionTasksOffset += data.list.length
    sessionTasksMore.value = data.list.length === data.page_size
  } catch {
    if (version === taskListVersion && !request.signal.aborted) sessionTasksError.value = '当前会话的任务读取失败，可刷新重试。'
  } finally { if (version === taskListVersion) sessionTasksLoading.value = false }
}
async function refreshActiveNotice(): Promise<void> {
  if (disposed || noticeLoading || document.hidden) return
  clearTimeout(noticeTimer); noticeLoading = true
  const request = new AbortController(); noticeController = request
  const timeout = setTimeout(() => request.abort(), 15_000)
  try {
    const pages = await Promise.all(activeTaskStatuses.map(status => api.opsTasks(undefined, 0, { status, limit: 1, signal: request.signal })))
    if (!disposed) { hasActiveTasks.value = pages.some(page => page.list.length > 0); noticeError.value = '' }
  } catch { if (!disposed) noticeError.value = '任务提醒暂未更新，可在任务记录中刷新查看。' }
  finally {
    clearTimeout(timeout); request.abort(); noticeLoading = false
    if (!disposed) noticeTimer = setTimeout(() => { void refreshActiveNotice() }, 30_000)
  }
}
function noticeVisibility(): void { if (!document.hidden) void refreshActiveNotice() }
function openTaskRecords(filter: 'all' | 'unlinked' | 'active' = 'all'): void { taskRecordFilter.value = filter; tasksOpen.value = true }
async function openTaskSession(id: string): Promise<void> { tasksOpen.value = false; await openSession(id) }
async function explainRecordedTask(task: OpsTask): Promise<void> {
  tasksOpen.value = false
  if (task.parent_turn_id && !task.link_pending) {
    await openSession(task.session_id)
    if (sessionID.value !== task.session_id) return
  } else newChat()
  explainTask(task.task_id)
}
async function refreshAll(): Promise<void> { await refreshSessions(); await Promise.all([refreshTasks(sessionID.value), refreshActiveNotice()]) }
function explainTask(id: string): void { draft.value = `请解释运维任务 ${id} 的历史结果，不重新查询设备。`; void send() }
const sessions = ref<RAGSession[]>([])
const sessionClock = ref(Date.now())
let sessionClockTimer: ReturnType<typeof setInterval> | undefined
const conversation = ref<HTMLElement>()
const conversationContent = ref<HTMLElement>()
const followLatest = ref(true)
let conversationObserver: ResizeObserver | undefined
let scrollFrame = 0
let previousScrollTop = 0

function scrollToLatest(force = false): void {
  if (force) followLatest.value = true
  void nextTick(() => {
    cancelAnimationFrame(scrollFrame)
    scrollFrame = requestAnimationFrame(() => {
      if (!followLatest.value || !turns.value.length || tasksOpen.value || verifyTarget.value || source.value || document.hidden) return
      const viewport = conversation.value
      if (!viewport) return
      viewport.scrollTo({ top: viewport.scrollHeight, behavior: 'auto' })
      previousScrollTop = viewport.scrollTop
    })
  })
}

function trackConversationScroll(): void {
  const viewport = conversation.value
  if (!viewport) return
  if (viewport.scrollTop < previousScrollTop - 2) followLatest.value = false
  else if (viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 80) followLatest.value = true
  previousScrollTop = viewport.scrollTop
}

function pauseFollowing(event: WheelEvent): void {
  if (event.deltaY < 0) followLatest.value = false
}
const turns = ref<(Partial<RAGTurn> & { question: string; error?: string; historical?: boolean })[]>([])
const sessionID = ref<string>()
const draft = ref('')
const route = useRoute()
watch(() => route.query.question, question => { if (typeof question === 'string') draft.value = question }, { immediate: true })
const pending = ref(false)
const loading = ref(false)
const listError = ref('')
const moreSessions = ref(false)
const source = ref<{ document: string; heading: string; content: string; version: string; page?: number }>()
const sourceError = ref('')
let controller: AbortController | undefined
let generation = 0
const verifyTarget = ref<{ session: string; turn: number }>()
const verifyDevices = ref<Device[]>([])
const verifyDevice = ref<number>()
const verifyChecks = ref<VerificationCheck[]>(['latest_telemetry', 'current_alarms'])
const verifyError = ref('')
const deviceWarning = ref('')
const deviceLoading = ref(false)
const verifying = ref(false)
const retryIDs = new Map<string, string>()
const examples = ['什么是 TCP 协议？', '设备日常巡检需要检查什么？', '第二台设备状态如何？', '帮我查一下平台的 Docker 部署方式', '搜索最近的光伏技术进展']
const chars = computed(() => draft.value.length)
async function refreshSessions(append = false): Promise<void> {
  try { const data = await api.chatSessions(append ? sessions.value.length : 0); sessions.value = append ? [...sessions.value, ...data.list] : data.list; moreSessions.value = data.list.length === data.page_size; listError.value = '' }
  catch (e) { listError.value = e instanceof Error ? e.message : '历史会话读取失败' }
}
const historyOpen = ref(sessionStorage.getItem('rag-history-open') !== '0')
const selectMode = ref(false)
const selected = ref<string[]>([])
const batchDeleting = ref(false)
const allSelected = computed(() => sessions.value.length > 0 && selected.value.length === sessions.value.length)
const someSelected = computed(() => selected.value.length > 0)
function toggleHistory(open?: boolean): void {
  historyOpen.value = open ?? !historyOpen.value
  try { sessionStorage.setItem('rag-history-open', historyOpen.value ? '1' : '0') } catch { /* 存储不可用时仅本次生效 */ }
}
function exitSelectMode(): void { selectMode.value = false; selected.value = [] }
function toggleSelect(id: string): void { selected.value = selected.value.includes(id) ? selected.value.filter(x => x !== id) : [...selected.value, id] }
function toggleSelectAll(): void { selected.value = allSelected.value ? [] : sessions.value.map(item => item.id) }
async function removeSelectedSessions(): Promise<void> {
  const ids = [...selected.value]
  if (!ids.length || batchDeleting.value) return
  try { await ElMessageBox.confirm(`将删除 ${ids.length} 段会话，删除后无法恢复。`, '批量删除会话', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' }) } catch { return }
  batchDeleting.value = true
  let failed = 0
  for (const id of ids) { try { await api.deleteChatSession(id) } catch { failed++ } }
  if (sessionID.value && ids.includes(sessionID.value)) newChat()
  exitSelectMode(); await refreshSessions(); batchDeleting.value = false
  if (failed) ElMessage.error(`${failed} 段会话删除失败，请刷新后重试`)
  else ElMessage.success(`已删除 ${ids.length} 段会话`)
}
function cancel(): void { if (pending.value) { const waiting = [...turns.value].reverse().find(turn => !turn.response && !turn.error); if (waiting) waiting.error = '已取消等待；已受理的任务继续运行，可刷新恢复任务状态。' } generation++; controller?.abort(); controller = undefined; pending.value = false }
function newChat(): void { cancel(); clearSessionTasks(); tasksOpen.value = false; followLatest.value = true; verifyTarget.value = undefined; sessionID.value = undefined; turns.value = []; draft.value = ''; source.value = undefined; loading.value = false; void nextTick(() => { conversation.value?.scrollTo({ top: 0 }); previousScrollTop = 0 }); void refreshActiveNotice() }
async function openSession(id: string): Promise<void> {
  cancel(); clearSessionTasks(); verifyTarget.value = undefined; const mine = generation; loading.value = true
  try { const data = await api.chatSession(id); if (mine === generation) { sessionID.value = id; turns.value = data.turns.map(turn => ({ ...turn, historical: true })); await refreshTasks(id); scrollToLatest(true) } }
  catch (e) { ElMessage.error(e instanceof Error ? e.message : '会话读取失败') }
  finally { if (mine === generation) loading.value = false }
}
async function removeSession(id: string): Promise<void> {
  try { await ElMessageBox.confirm('删除后无法恢复这段会话。', '删除会话', { type: 'warning' }) } catch { return }
  try { await api.deleteChatSession(id); if (sessionID.value === id) newChat(); await refreshSessions() }
  catch (e) { ElMessage.error(e instanceof Error ? e.message : '删除失败') }
}
async function send(): Promise<void> {
  if (pending.value || !draft.value.trim() || chars.value > 32000) return
  const question = draft.value.trim(); draft.value = ''; pending.value = true
  const index = turns.value.push({ question }) - 1
  scrollToLatest(true)
  const mine = ++generation; controller = new AbortController()
  try {
    const response = await api.chat(question, sessionID.value, controller.signal)
    if (mine !== generation) return
    sessionID.value = response.session_id; turns.value[index]!.id = response.turn_id; turns.value[index]!.response = response; await refreshSessions(); await refreshTasks(response.session_id); void refreshActiveNotice()
  } catch (e) { if (mine === generation) turns.value[index]!.error = e instanceof Error ? e.message : '服务不可用' }
  finally { if (mine === generation) { pending.value = false; controller = undefined } }
}
async function copy(text: string): Promise<void> {
  try { await navigator.clipboard.writeText(text); ElMessage.success('已复制') } catch { ElMessage.error('当前浏览器无法复制，请手动选择文本') }
}
async function showSource(id: string): Promise<void> {
  sourceError.value = ''
  try { source.value = await api.ragSource(id) } catch (e) { sourceError.value = e instanceof Error ? e.message : '来源读取失败' }
}
async function openVerify(turn: Partial<RAGTurn>): Promise<void> {
  const id = turn.id ?? turn.response?.turn_id
  if (!sessionID.value || !id || pending.value) return
  verifyTarget.value = { session: sessionID.value, turn: id }; verifyError.value = ''; deviceWarning.value = ''
  verifyDevices.value = []; verifyDevice.value = undefined; verifyChecks.value = ['latest_telemetry', 'current_alarms']; deviceLoading.value = true
  const target = verifyTarget.value
  try {
    const devices = new Map<number, Device>()
    for (let page = 1; page <= 20; page++) {
      const data = await api.devices(page, 100)
      if (verifyTarget.value !== target) return
      for (const row of data.list) devices.set(row.id, row)
      if (devices.size >= data.total) break
      if (!data.list.length || page === 20) { deviceWarning.value = '设备列表未完整加载，仅可选择已加载设备；不推断全量序数。'; break }
    }
    verifyDevices.value = [...devices.values()].sort((a, b) => a.id - b.id)
    const actual = new Set(turn.response?.tool_calls.filter(c => c.status === 'ok').map(c => c.arguments.device_id).filter(x => typeof x === 'number') as number[])
    if (actual.size === 1) { const candidate = [...actual][0]; if (verifyDevices.value.some(d => d.id === candidate)) verifyDevice.value = candidate }
  } catch (e) { if (verifyTarget.value === target) verifyError.value = e instanceof Error ? e.message : '设备列表读取失败' }
  finally { if (verifyTarget.value === target) deviceLoading.value = false }
}
async function runVerify(): Promise<void> {
  const target = verifyTarget.value
  if (!target || !verifyDevice.value || !verifyChecks.value.length || pending.value) return
  const checks = [...verifyChecks.value].sort()
  const signature = JSON.stringify({ ...target, device: verifyDevice.value, checks })
  const vid = retryIDs.get(signature) || crypto.randomUUID(); retryIDs.set(signature, vid)
  verifying.value = true; pending.value = true; verifyError.value = ''
  const mine = ++generation; controller = new AbortController()
  try {
    const response = await api.verifyChat(target.session, { parent_turn_id: target.turn, device_id: verifyDevice.value, checks, verification_id: vid }, controller.signal)
    if (mine !== generation || sessionID.value !== target.session) return
    if (!turns.value.some(t => (t.id ?? t.response?.turn_id) === response.turn_id)) turns.value.push({ id: response.turn_id, kind: 'verification', parent_turn_id: target.turn, question: `主动核验 · ${response.device?.device_code || verifyDevice.value}`, response })
    retryIDs.delete(signature); verifyTarget.value = undefined; await refreshSessions()
  } catch (e) { if (mine === generation) verifyError.value = (e instanceof Error ? e.message : '核验失败') + '。重试会沿用同一请求编号。' }
  finally { verifying.value = false; if (mine === generation) { pending.value = false; controller = undefined } }
}
watch([turns, recoveredTasks], () => scrollToLatest(), { deep: true, flush: 'post' })
onMounted(() => {
  void refreshSessions(); void refreshActiveNotice()
  document.addEventListener('visibilitychange', noticeVisibility)
  sessionClockTimer = setInterval(() => { sessionClock.value = Date.now() }, 60_000)
  previousScrollTop = conversation.value?.scrollTop || 0
  conversationObserver = new ResizeObserver(() => scrollToLatest())
  if (conversationContent.value) conversationObserver.observe(conversationContent.value)
  if (conversation.value) conversationObserver.observe(conversation.value)
})
onUnmounted(() => {
  disposed = true; cancel(); clearSessionTasks(); noticeController?.abort(); clearTimeout(noticeTimer)
  document.removeEventListener('visibilitychange', noticeVisibility)
  clearInterval(sessionClockTimer); cancelAnimationFrame(scrollFrame)
  conversationObserver?.disconnect()
})
</script>

<template>
  <div class="chat-page rag-page">
    <section class="page-head">
      <div><h1>智能助手</h1><p>技术问答 · 内部资料 · 联网搜索 · 平台查询</p></div>
      <div class="rag-page-actions">
        <el-button @click="openTaskRecords()">任务记录</el-button>
        <el-button type="primary" @click="newChat">新会话</el-button>
      </div>
    </section>
    <div class="rag-layout">
      <main class="rag-conversation" v-loading="loading">
        <div ref="conversation" class="rag-message-list" role="region" aria-label="对话内容" tabindex="0" @scroll.passive="trackConversationScroll" @wheel.passive="pauseFollowing">
        <div ref="conversationContent" class="rag-thread">
        <div v-if="!sessionID && !turns.length && hasActiveTasks" class="rag-task-notice" role="status"><span>有尚未完成的运维任务</span><el-button text size="small" @click="openTaskRecords('active')">查看任务</el-button></div>
        <p v-if="!sessionID && !turns.length && noticeError" class="rag-muted rag-thread-note">{{ noticeError }}</p>
        <section v-if="!turns.length" class="chat-empty">
          <div class="chat-empty-inner">
            <p class="chat-empty-eyebrow">值班问答 · 已接入平台数据</p>
            <h2>有什么想了解的？</h2>
            <p class="chat-empty-desc">可以询问技术、新能源设备和平台问题，也可以查项目资料与最新技术信息。生活与闲聊问题暂不提供内容回答。</p>
            <div class="chat-examples">
              <button v-for="item in examples" :key="item" type="button" class="chat-example" @click="draft = item">{{ item }}</button>
            </div>
          </div>
        </section>
        <article v-for="(turn, index) in turns" :id="`turn-${turn.id ?? turn.response?.turn_id ?? index}`" :key="index" class="chat-turn" :class="{ verification: turn.kind === 'verification', task: !!turn.response?.task_cards?.length }">
          <header class="chat-turn-user">
            <span class="chat-q-label">{{ turn.kind === 'verification' ? '主动核验' : '你' }}</span>
            <span v-if="turn.response?.status === 'degraded'" class="chat-error">{{ turn.kind === 'verification' ? '核验含错误或缺失' : '服务故障' }}</span>
            <p class="chat-question">{{ turn.question }}</p>
          </header>
          <p v-if="turn.error" class="chat-error" role="alert">{{ turn.error }}</p>
          <div v-if="turn.response" class="chat-turn-answer">
            <template v-if="turn.kind === 'verification'">
              <p class="chat-verify-intro">程序只读核验 · {{ turn.response.device?.device_code || '设备台账未取得' }}。本记录展示接口实际结果，不代表完整诊断。</p>
              <a :href="`#turn-${turn.parent_turn_id}`">查看原回答 #{{ turn.parent_turn_id }}</a>
              <VerificationReport v-if="turn.response.verification_results" :results="turn.response.verification_results" />
            </template>
            <template v-else>
              <p v-if="programReplyLabel(turn.response)" class="rag-muted">{{ programReplyLabel(turn.response) }}</p>
              <details v-if="turn.response.task_cards?.length && !turn.response.business_quality && turn.response.answer_markdown" class="rag-receipt"><summary>初始受理回执 · 原始消息，任务状态见下方</summary><div class="rag-markdown" v-html="renderAnswer(turn.response.answer_markdown)" /></details>
              <div v-else-if="turn.response.answer_markdown" class="rag-markdown" v-html="renderAnswer(turn.response.answer_markdown)" />
              <p v-else class="chat-error">本轮未取得模型回答，请稍后重试。</p>
            </template>
            <BusinessQuality :quality="turn.response.business_quality" :historical="turn.historical || index < turns.length - 1" />
            <p v-if="!turn.response.business_quality && turn.historical && hasBusinessEvidence(turn.response.evidence)" class="rag-muted">历史正文保留原文，未按新业务规则校验。</p>
            <OpsTaskCard v-for="task in (turn.response.task_cards || []).filter(task => task.session_id === sessionID)" :key="task.task_id" :initial="task" :historical="turn.historical" @explain="explainTask" @source="showSource" />
            <BusinessEvidence v-if="turn.response.evidence && !programReplyLabel(turn.response) && (!turn.response.task_cards?.length || hasBusinessEvidence(turn.response.evidence))" :evidence="turn.response.evidence" :historical="turn.historical || index < turns.length - 1" />
            <div class="chat-turn-actions">
              <el-button v-if="turn.kind !== 'verification' && canVerify(turn.response) && (turn.id || turn.response.turn_id)" size="small" :disabled="pending" @click="openVerify(turn)">补查实时数据</el-button>
              <el-button v-if="turn.response.answer_markdown" text size="small" @click="copy(turn.response.answer_markdown)">{{ turn.response.task_cards?.length && !turn.response.business_quality ? '复制原始回执' : '复制回答' }}</el-button>
            </div>
            <p v-for="warning in turn.response.warnings" :key="warning" class="chat-limit">{{ warning }}</p>
            <details v-if="turn.response.sources.length" class="rag-details"><summary>参考来源 · {{ turn.response.sources.length }}</summary><div v-for="item in turn.response.sources" :key="item.id" class="rag-source"><span>{{ item.kind === 'web' ? '网页' : item.kind === 'document' ? '知识库' : '平台数据' }} · {{ item.title }}{{ item.chapter ? ` / ${item.chapter}` : '' }}</span><el-button v-if="item.kind === 'document'" text @click="showSource(item.id)">阅读章节</el-button><a v-if="item.kind === 'web' && sourceLink(item.url)" :href="sourceLink(item.url)!" target="_blank" rel="noopener noreferrer">打开网页</a><small>{{ dateTime(item.collected_at) }}{{ item.published_at ? ` · 发布 ${dateTime(item.published_at)}` : '' }}</small></div></details>
            <details v-if="turn.response.tool_calls.length" class="rag-details"><summary>工具调用 · {{ turn.response.tool_calls.length }}</summary><div v-for="(call, callIndex) in turn.response.tool_calls" :key="callIndex"><p>{{ call.name }} · {{ call.status }}</p><pre>{{ JSON.stringify({ arguments: call.arguments, result: call.result, error: call.error }, null, 2) }}</pre></div></details>
            <footer class="chat-turn-foot">{{ programReplyLabel(turn.response) || turn.response.model }} · {{ turn.response.request_id }}</footer>
          </div>
          <p v-else-if="!turn.error" class="chat-waiting">{{ pending && index === turns.length - 1 ? '正在思考，按需查询资料与工具…' : '等待已取消，可以继续提问。' }}</p>
        </article>
        <template v-if="sessionID">
          <OpsTaskCard v-for="task in recoveredTasks" :key="`${sessionID}:${task.task_id}`" :initial="task" historical @explain="explainTask" @source="showSource" />
          <p v-if="sessionTasksError" class="chat-error" role="alert">{{ sessionTasksError }}</p>
          <el-button v-if="sessionTasksMore" text :loading="sessionTasksLoading" @click="refreshTasks(sessionID, true)">加载此会话的更多任务</el-button>
        </template>
        <div class="rag-conversation-end" aria-hidden="true" />
        </div>
        </div>
        <div class="rag-compose-wrap">
          <button v-if="!followLatest && turns.length" type="button" class="rag-jump-latest" @click="scrollToLatest(true)">回到最新对话 ↓</button>
          <section class="rag-compose">
            <el-input v-model="draft" type="textarea" :autosize="{ minRows: 2, maxRows: 10 }" placeholder="输入问题，Ctrl / ⌘ + Enter 发送" @keydown.ctrl.enter.prevent="send" @keydown.meta.enter.prevent="send" />
            <div class="rag-compose-actions">
              <el-button v-if="pending" @click="cancel">取消等待</el-button>
              <el-button class="rag-send" type="primary" circle :disabled="pending || !draft.trim() || chars > 32000" aria-label="发送" @click="send"><el-icon><Promotion /></el-icon></el-button>
            </div>
          </section>
        </div>
      </main>
      <div class="rag-history-wrap">
        <button type="button" class="rag-history-handle" :aria-label="historyOpen ? '收起历史会话' : '展开历史会话'" :title="historyOpen ? '收起历史会话' : '展开历史会话'" @click="toggleHistory()"><el-icon><ArrowRight v-if="historyOpen" /><ArrowLeft v-else /></el-icon></button>
      <aside class="surface rag-sidebar" :class="{ 'is-collapsed': !historyOpen }">
        <div class="rag-sidebar-inner">
        <div class="rag-sidebar-head">
          <strong>历史会话</strong>
          <div v-if="!selectMode" class="rag-sidebar-tools">
            <el-button text size="small" @click="refreshAll()">刷新</el-button>
            <el-button text size="small" :disabled="!sessions.length" @click="selectMode = true">管理</el-button>
          </div>
          <div v-else class="rag-sidebar-tools">
            <el-checkbox :model-value="allSelected" :indeterminate="someSelected && !allSelected" size="small" @change="toggleSelectAll">全选</el-checkbox>
            <el-button text size="small" type="danger" :disabled="!selected.length || batchDeleting" :loading="batchDeleting" @click="removeSelectedSessions()">删除{{ selected.length ? ` (${selected.length})` : '' }}</el-button>
            <el-button text size="small" :disabled="batchDeleting" @click="exitSelectMode()">取消</el-button>
          </div>
        </div>
        <div class="rag-session-list" role="region" aria-label="历史会话列表" tabindex="0" @wheel.stop>
        <p v-if="listError" class="chat-error">{{ listError }}</p><p v-if="!sessions.length && !listError" class="rag-muted rag-sidebar-empty">开始聊天后，会话会自动保存。</p>
        <div v-for="item in sessions" :key="item.id" class="rag-session" :class="{ active: item.id === sessionID && !selectMode, selecting: selectMode, checked: selected.includes(item.id) }">
          <el-checkbox v-if="selectMode" :model-value="selected.includes(item.id)" size="small" :aria-label="`选择会话：${item.title}`" @change="toggleSelect(item.id)" />
          <button type="button" class="rag-session-title" :title="item.title" :aria-current="item.id === sessionID ? 'true' : undefined" @click="selectMode ? toggleSelect(item.id) : openSession(item.id)">{{ item.title }}</button>
          <div v-if="!selectMode" class="rag-session-meta">
            <time class="rag-session-time" :datetime="item.updated_at || item.created_at" :title="`最后对话：${dateTime(item.updated_at || item.created_at)}`">{{ sessionRecency(item.updated_at || item.created_at, sessionClock) }}</time>
            <button type="button" class="rag-session-delete" :aria-label="`删除会话：${item.title}`" title="删除会话" :disabled="pending && item.id === sessionID" @click="removeSession(item.id)"><el-icon><Delete /></el-icon></button>
          </div>
        </div>
        <el-button v-if="moreSessions" text size="small" class="rag-load-more" @click="refreshSessions(true)">加载更多</el-button>
        </div>
        </div>
      </aside>
      </div>
    </div>
    <p v-if="sourceError" class="chat-error">{{ sourceError }}</p>
    <TaskRecordsPanel v-model="tasksOpen" :initial-filter="taskRecordFilter" @open-session="openTaskSession" @explain="explainRecordedTask" @source="showSource" @refresh-notice="refreshActiveNotice" />
    <el-dialog :model-value="!!verifyTarget" title="补查实时数据" width="min(640px, 94vw)" :close-on-click-modal="!verifying" :close-on-press-escape="!verifying" :show-close="!verifying" @close="verifyTarget = undefined">
      <p>读取当前设备列表，按 ID 升序展示。确认后才查询遥测、告警及所选预测；结果追加，原回答保留。</p>
      <p v-if="deviceWarning" class="chat-limit">{{ deviceWarning }}</p>
      <el-select v-model="verifyDevice" :loading="deviceLoading" :disabled="verifying" filterable placeholder="请选择设备，不确定指代时请人工选择" style="width:100%"><el-option v-for="device in verifyDevices" :key="device.id" :value="device.id" :label="`ID ${device.id} · ${device.device_code} · ${device.name}`" /></el-select>
      <fieldset class="verify-checks" :disabled="verifying"><legend>查询项目</legend><label><input v-model="verifyChecks" type="checkbox" value="latest_telemetry" />最新遥测</label><label><input v-model="verifyChecks" type="checkbox" value="current_alarms" />当前告警</label><label><input v-model="verifyChecks" type="checkbox" value="prediction" />预测</label></fieldset>
      <p v-if="verifyError" class="chat-error" role="alert">{{ verifyError }}</p>
      <template #footer><el-button :disabled="verifying" @click="verifyTarget = undefined">取消</el-button><el-button type="primary" :loading="verifying" :disabled="pending || deviceLoading || !verifyDevice || !verifyChecks.length" @click="runVerify">确认补查并追加记录</el-button></template>
    </el-dialog>
    <el-dialog :model-value="!!source" title="知识库来源" width="min(800px, 94vw)" @close="source = undefined"><template v-if="source"><h3>{{ source.document }} · {{ source.heading }}</h3><p v-if="source.page">第 {{ source.page }} 页</p><pre class="rag-source-content">{{ source.content }}</pre><small>版本 {{ source.version }}</small></template></el-dialog>
  </div>
</template>

<style scoped>
/* ---------- 页头与布局 ---------- */
.chat-page{display:flex;flex-direction:column;gap:20px}
.rag-page-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.rag-page-actions .el-button+.el-button{margin-left:0}
.rag-page-actions .el-button{border-radius:10px}
.rag-page{flex:1;min-height:0;gap:14px;overflow:hidden}
.rag-page>.page-head{flex-shrink:0;margin-bottom:0}
.rag-layout{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:stretch;flex:1;min-height:0;min-width:0;grid-template-rows:minmax(0,1fr)}
.rag-conversation{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
.rag-message-list{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior-y:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--line-strong) transparent;padding-right:4px}
.rag-message-list:focus-visible{outline:1px solid var(--brand-line);outline-offset:-1px}

/* 居中线程：对话内容限宽居中，避免长行难读 */
.rag-thread{max-width:760px;margin:0 auto;padding:6px 8px 20px}
.rag-thread-note{max-width:760px;margin:0 auto 8px}
.rag-task-notice{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:6px 14px;margin:0 auto 12px;max-width:760px;border:1px solid var(--brand-line);border-radius:10px;background:var(--brand-soft);color:var(--ink-2);font-size:12px}

/* ---------- 空状态欢迎区 ---------- */
.chat-empty{display:grid;place-items:center;min-height:min(560px,100%);padding:40px 0}
.chat-empty-inner{max-width:520px;text-align:center}
.chat-empty-eyebrow{display:inline-block;font-size:11px;letter-spacing:2px;color:var(--brand);border:1px solid var(--brand-line);background:var(--brand-soft);border-radius:var(--r-full);padding:4px 12px;margin-bottom:18px}
.chat-empty h2{font-size:24px;font-weight:700;color:var(--ink);margin:0 0 10px}
.chat-empty-desc{color:var(--ink-3);font-size:13px;line-height:1.8;margin:0}
.chat-examples{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin-top:26px}
.chat-example{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink-2);padding:9px 15px;cursor:pointer;font:inherit;font-size:12.5px;transition:border-color .15s,color .15s,background .15s}
.chat-example:hover{border-color:var(--brand-line);background:var(--brand-soft);color:var(--brand)}

/* ---------- 对话轮次 ---------- */
.chat-turn{margin-bottom:26px}
.chat-turn-user{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;justify-content:flex-end;margin-bottom:6px}
.chat-q-label{font-size:11px;font-weight:700;color:var(--brand);letter-spacing:1px}
.chat-question{margin:0;max-width:82%;padding:11px 18px;background:var(--card-2);border:1px solid var(--line-strong);border-radius:16px 16px 5px 16px;color:var(--ink);font-size:14.5px;font-weight:500;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}
.chat-turn-user .chat-error{font-size:11px}
.chat-turn-answer{padding:2px 0 2px 2px}
.chat-turn.verification .chat-q-label{color:var(--warning)}
.chat-verify-intro{font-size:12.5px;color:var(--ink-2)}
.chat-turn.task .chat-question{font-size:13.5px;font-weight:500;color:var(--ink-2)}
.chat-turn-actions{display:flex;gap:8px;align-items:center;margin-top:12px}
.chat-turn-actions .el-button+.el-button{margin-left:0}
.chat-turn-foot{font-size:11px;color:var(--ink-3);overflow-wrap:anywhere;margin-top:14px;font-variant-numeric:tabular-nums}
.chat-error{color:var(--danger);font-size:13px}
.chat-limit,.chat-waiting{color:var(--ink-3);font-size:13px}
.chat-waiting{padding-left:2px;font-style:normal}
.rag-conversation-end{height:1px;scroll-margin-bottom:20px}

/* 回答正文与附属区块 */
.rag-markdown{line-height:1.8;overflow-wrap:anywhere;font-size:14px}
.rag-markdown :deep(pre),.rag-details pre,.rag-source-content{overflow:auto;white-space:pre-wrap;background:var(--bg);padding:14px;border-radius:var(--r-md);font-size:12px}
.rag-markdown :deep(table){border-collapse:collapse;width:100%}
.rag-markdown :deep(th),.rag-markdown :deep(td){border:1px solid var(--line);padding:8px;text-align:left}
.rag-markdown :deep(a){color:var(--brand)}
.rag-receipt{font-size:12px;color:var(--ink-3);margin:12px 0}.rag-receipt summary{cursor:pointer}
.rag-details{border-top:1px solid var(--line-soft);padding:10px 0;margin-top:10px}
.rag-details summary{cursor:pointer;color:var(--ink-2);font-size:12.5px}
.rag-source{padding:10px 0;display:flex;flex-wrap:wrap;gap:12px;align-items:center;font-size:12.5px}
.rag-source small{flex-basis:100%;color:var(--ink-3)}
.rag-muted{color:var(--ink-3);font-size:12px}

/* ---------- 输入区 ---------- */
.rag-compose-wrap{position:relative;flex-shrink:0;padding-top:4px}
.rag-compose{max-width:760px;margin:0 auto;padding:10px 16px;border:1px solid var(--line-strong);border-radius:20px;background:var(--card-2);transition:border-color .15s,box-shadow .15s}
.rag-compose:focus-within{border-color:var(--brand-line);box-shadow:0 0 0 3px var(--brand-soft)}
.rag-compose :deep(.el-textarea__inner){background:transparent !important;box-shadow:none !important;border:0 !important;padding:4px 2px;font-size:14px}
.rag-compose-actions{display:flex;gap:10px;justify-content:flex-end;align-items:center;margin-top:8px}
.rag-send{width:34px;height:34px;font-size:15px}
.rag-jump-latest{position:absolute;right:50%;transform:translateX(50%);top:-14px;z-index:5;padding:5px 12px;border:1px solid var(--line-strong);border-radius:var(--r-full);background:var(--card-2);color:var(--ink);font:inherit;font-size:12px;box-shadow:var(--shadow-md)}
.rag-jump-latest:hover{border-color:var(--brand);color:var(--brand)}
.rag-jump-latest:focus-visible{outline:2px solid var(--brand);outline-offset:2px}

/* ---------- 历史会话侧栏（抽屉式：边缘箭头收起/展开） ---------- */
.rag-history-wrap{position:relative;min-height:0;display:flex}
.rag-history-handle{position:absolute;left:-12px;top:50%;transform:translateY(-50%);z-index:6;display:grid;place-items:center;width:24px;height:52px;padding:0;border:1px solid var(--line-strong);border-right:0;border-radius:12px 0 0 12px;background:var(--card-2);color:var(--ink-3);transition:color .15s,border-color .15s,background .15s}
.rag-history-handle:hover{color:var(--ink);border-color:var(--line-strong);background:var(--hover)}
.rag-history-handle:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rag-history-handle .el-icon{font-size:13px}
.rag-sidebar{position:static;display:flex;flex-direction:column;min-height:0;max-height:none;width:280px;padding:0;border-width:1px;border-radius:12px;overflow:hidden;transition:width .22s ease,border-color .22s ease}
.rag-sidebar.is-collapsed{width:10px;border-radius:0;border-color:transparent;border-left:1px solid var(--line-strong)}
.rag-sidebar-inner{display:flex;flex-direction:column;min-height:0;width:280px;flex:1 1 auto;padding:12px 10px}
.chat-turn :deep(.ops-task){border-radius:12px}
.rag-sidebar-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px;padding:0 4px;flex-shrink:0;min-width:0}
.rag-sidebar-head strong{font-size:13px;font-weight:700;letter-spacing:.5px;flex:none}
.rag-sidebar-tools{display:flex;align-items:center;gap:2px;min-width:0;font-size:12px}
.rag-sidebar-tools .el-button+.el-button{margin-left:0}
.rag-sidebar-tools .el-button{font-size:12px;border-radius:8px}
.rag-sidebar-tools .el-checkbox{height:auto;margin:0;display:inline-flex;align-items:center;font-size:12px}
.rag-sidebar-tools .el-checkbox__label{padding-left:4px;font-size:12px}
.rag-sidebar-empty{padding:0 6px}
.rag-session-list{flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior-y:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--line-strong) transparent;padding:0 2px}
.rag-session-list:focus-visible{outline:1px solid var(--brand-line);outline-offset:-1px}
.rag-session{display:flex;align-items:center;gap:8px;position:relative;min-height:38px;padding:0 8px;border-radius:9px;margin-bottom:2px;color:var(--ink-2);transition:background .15s}
.rag-session:hover{background:var(--hover)}
.rag-session.active{background:var(--brand-soft)}
.rag-session.active::before{content:"";position:absolute;left:-2px;top:9px;bottom:9px;width:3px;border-radius:2px;background:var(--brand)}
.rag-session.selecting{cursor:pointer}
.rag-session.selecting .rag-session-title{color:var(--ink-2)}
.rag-session.checked{background:var(--brand-soft)}
.rag-session-title{align-self:stretch;background:none;border:0;color:var(--ink);text-align:left;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;padding:9px 0;min-width:0;font:inherit;font-size:13px;font-weight:400;line-height:20px}
.rag-session.active .rag-session-title{font-weight:600}
.rag-session-meta{position:relative;flex:0 0 52px;height:34px;display:flex;align-items:center;justify-content:flex-end}
.rag-session-time{font-size:11px;line-height:16px;font-variant-numeric:tabular-nums;color:var(--ink-3);white-space:nowrap}
.rag-session-delete{position:absolute;right:0;top:3px;display:grid;place-items:center;width:28px;height:28px;border:0;border-radius:var(--r-sm);background:var(--hover);color:var(--ink-2);opacity:0;pointer-events:none;visibility:hidden}
.rag-session:hover .rag-session-delete,.rag-session:has(:focus-visible) .rag-session-delete{opacity:1;pointer-events:auto;visibility:visible}
.rag-session:hover .rag-session-time,.rag-session:has(:focus-visible) .rag-session-time{opacity:0}
.rag-session-delete:hover:not(:disabled){color:var(--danger);background:var(--danger-soft)}
.rag-session-delete:disabled{cursor:not-allowed;color:var(--ink-3)}
.rag-session-title:focus-visible,.rag-session-delete:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rag-load-more{margin:4px auto 0;display:block}

/* ---------- 弹窗补充 ---------- */
.verify-checks{margin-top:16px;border:1px solid var(--line);display:flex;flex-wrap:wrap;gap:16px}.verify-checks label{display:flex;align-items:center;gap:6px}.verification-result{margin-top:12px}.verification-result pre{max-height:360px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}

/* ---------- 响应式 ---------- */
@media(max-width:900px){
  .rag-layout{grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) auto}
  .rag-sidebar{position:static;max-height:min(240px,32dvh)}
  .rag-thread{padding:4px 2px 14px}
  .chat-question{max-width:100%}
  .rag-compose-actions{flex-wrap:wrap}
}
@media(hover:none){
  .rag-session-delete{opacity:1;visibility:visible;pointer-events:auto}
  .rag-session-time{margin-right:30px}
  .rag-session-meta{flex-basis:84px}
  .rag-session:focus-within .rag-session-time{opacity:1}
}
</style>
