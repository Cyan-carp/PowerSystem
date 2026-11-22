<script setup lang="ts">
/**
 * 智能助手：会话 + 运行（V4 runs）。
 *
 * 与覆盖前那版的差别只在形态，行为契约没变：
 * - 发问先拿 receipt（POST /agent/runs，带 Idempotency-Key），再按 run_id 订阅
 *   SSE 事件流；正文、进度、审批动作、产物都从同一条流上长出来。
 * - 断线不取消任务。重新进页面时只对最近的轮次重新挂流（有界），历史轮次直接
 *   用会话里存的 answered 文本 + 来源渲染，避免一次开十几条 SSE。
 * - 切会话/新建会话会让旧回调失效（generation 闸门），迟到的快照不会写进新会话。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Drawer from '../components/Drawer.vue'
import Icon from '../components/Icon.vue'
import RunAnswer from '../components/RunAnswer.vue'
import SourceList from '../components/SourceList.vue'
import TaskRecordsPanel from '../components/TaskRecordsPanel.vue'
import { api } from '../lib/api'
import type { AgentSession, AgentTurn, AgentTurnResponse, AttachmentResult, KnowledgeChapter, RAGSource } from '../lib/api'
import { useAssistantAttachments } from '../lib/useAssistantAttachments'
import { cancelRun, consumeRun, createRun, getRun, isTerminalStatus, RunHttpError } from '../lib/agent-runs'
import type { RunConnectionState, RunInput, RunSnapshot } from '../lib/agent-runs'
import { currentUser } from '../lib/auth'
import { across, clock } from '../lib/format'
import { renderAnswer } from '../lib/task-labels'
import { askConfirm, toast } from '../lib/toast'

interface Turn {
  localID: string
  question: string
  at: string
  /** 会话里存的回答元数据（正文、来源、模型）；运行快照不含来源 */
  response?: AgentTurnResponse
  attachmentResult?: AttachmentResult
  runId?: string
  run?: RunSnapshot
  connection?: RunConnectionState
  error?: string
  cancelling?: boolean
  /** 受理凭据：重试时复用同一把 Idempotency-Key，服务端据此去重 */
  admission?: { key: string; input: RunInput }
}

const turns = ref<Turn[]>([])
const sessions = ref<AgentSession[]>([])
const sessionID = ref<string | null>(null)
const sessionTitle = ref('新会话')
const loadingSession = ref(false)
const loadingList = ref(false)
const listError = ref('')

const draft = ref('')
const busy = ref(false)
const scroller = ref<HTMLElement | null>(null)
const inputter = ref<HTMLTextAreaElement | null>(null)
const attachmentPicker = ref<HTMLInputElement | null>(null)
const attachments = useAssistantAttachments()
const { files: attachmentFiles, selected: chosenAttachments, parsing: parsingAttachment,
  error: attachmentError, searching: searchingAttachments, active: attachmentMode } = attachments
const recordsOpen = ref(false)

/** 读章节抽屉：知识库来源点「阅读章节」后在这里展开全文 */
const sourceDoc = ref<KnowledgeChapter | null>(null)
const sourceLoading = ref(false)
const sourceError = ref('')
let sourceRequest: AbortController | null = null

const closeSource = () => {
  sourceRequest?.abort()
  sourceRequest = null
  sourceDoc.value = null
  sourceLoading.value = false
  sourceError.value = ''
}

const suggestions = [
  '哪个场站今天出力下降最明显？',
  '未处理告警里最久的是哪一条？',
  '哪些设备的健康分低于 80？',
]

const historyCount = computed(() => sessions.value.length)
/** 有运行在进行时才提示「后台继续运行」，避免空会话也挂个提示 */
const streaming = computed(() => turns.value.some((turn) => turn.run && !isTerminalStatus(turn.run.status)))

const subscriptions = new Map<string, AbortController>()
let generation = 0

const sessionKey = () => `assistant.session:${currentUser.value?.id || 'anonymous'}`

const scrollDown = async () => {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

const useSuggestion = (text: string) => {
  draft.value = text
  inputter.value?.focus()
}

/* ---------------- 会话列表（GET /agent/sessions） ---------------- */

const archivedList = ref(false)
const sessionWriting = ref(false)
let listGeneration = 0
const refreshSessions = async () => {
  const mine = ++listGeneration
  loadingList.value = true
  try {
    const payload = await api.sessions(0, archivedList.value)
    if (mine !== listGeneration) return
    sessions.value = payload.list
    listError.value = ''
  } catch (error) {
    if (mine === listGeneration) listError.value = String(error)
  } finally {
    if (mine === listGeneration) loadingList.value = false
  }
}
const showSessionList = (archived: boolean) => {
  if (sessionWriting.value || archivedList.value === archived) return
  archivedList.value = archived
  sessions.value = []
  void refreshSessions()
}
const archiveSession = async (session: AgentSession) => {
  if (sessionWriting.value) return
  sessionWriting.value = true
  const archived = !archivedList.value
  try {
    await api.archiveSession(session.id, archived)
    await refreshSessions()
    toast('ok', archived ? '会话已归档' : '会话已恢复')
  } catch (error) {
    listError.value = `操作失败：${String(error)}`
  } finally {
    sessionWriting.value = false
  }
}

const stopAll = () => {
  subscriptions.forEach((controller) => controller.abort())
  subscriptions.clear()
}

const newChat = () => {
  stopAll()
  attachments.reset()
  generation += 1
  sessionID.value = null
  sessionTitle.value = '新会话'
  turns.value = []
  draft.value = ''
  closeSource()
  try {
    sessionStorage.removeItem(sessionKey())
  } catch {
    /* 存储不可用时只影响刷新恢复 */
  }
  void nextTick(() => inputter.value?.focus())
}

const mapTurn = (turn: AgentTurn, index: number): Turn => ({
  localID: `history-${sessionID.value}-${index}-${turn.id ?? ''}`,
  question: turn.question,
  at: turn.created_at || new Date().toISOString(),
  response: turn.response || undefined,
  attachmentResult: turn.attachment_result,
  runId: turn.run_id || undefined,
})

const openSession = async (session: AgentSession) => {
  if (loadingSession.value) return
  stopAll()
  attachments.cancel()
  generation += 1
  const mine = generation
  loadingSession.value = true
  try {
    const payload = await api.session(session.id)
    if (mine !== generation) return
    sessionID.value = session.id
    sessionTitle.value = session.title || '未命名会话'
    turns.value = payload.turns.map(mapTurn)
    draft.value = ''
    attachments.reset()
    attachments.restore(payload.attachments ?? [], payload.turns)
    try {
      sessionStorage.setItem(sessionKey(), session.id)
    } catch {
      /* 存储不可用时只影响刷新恢复 */
    }
    // 只重挂最近两轮：更早的运行几乎都已终态，逐条 GET 只是白花请求
    for (const turn of turns.value.slice(-2)) {
      if (turn.runId) void hydrate(turn, mine)
    }
    await scrollDown()
  } catch (error) {
    if (mine === generation) listError.value = `会话读取失败：${String(error)}`
  } finally {
    if (mine === generation) loadingSession.value = false
  }
}

/** 从任务记录的「打开关联会话」进来：优先用已加载的列表，找不到就重拉一次 */
const openSessionById = async (id: string) => {
  recordsOpen.value = false
  const cached = sessions.value.find((item) => item.id === id)
  if (cached) {
    await openSession(cached)
    return
  }
  await refreshSessions()
  const found = sessions.value.find((item) => item.id === id)
  if (found) await openSession(found)
  else await openSession({ id, title: '历史会话', created_at: '', updated_at: '' })
}

const removeSession = async (session: AgentSession) => {
  if (sessionWriting.value) return
  const ok = await askConfirm({
    title: '删除会话',
    body: `将删除「${session.title || '未命名会话'}」及其全部轮次，删除后无法恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok || sessionWriting.value) return
  sessionWriting.value = true
  try {
    await api.deleteSession(session.id)
    await refreshSessions()
    if (sessionID.value === session.id) newChat()
    toast('ok', '会话已删除')
  } catch (error) {
    listError.value = `删除失败：${String(error)}`
  } finally {
    sessionWriting.value = false
  }
}

/* ---------------- 运行：受理 / 订阅 / 控制 ---------------- */

const withinGeneration = (mine: number) => mine === generation

/** 快照只按 last_seq 前进，迟到的旧快照不会覆盖新的 */
function applySnapshot(turn: Turn, snapshot: RunSnapshot): void {
  if (!turns.value.includes(turn)) return
  if (turn.run && BigInt(snapshot.last_seq) < BigInt(turn.run.last_seq)) return
  turn.run = snapshot
  turn.error = ''
}

/**
 * 按 run_id 重新挂上事件流。先读一次权威快照（可能已经终态），再决定是否订阅。
 * 绑定不一致（run / turn / session 任一不同）一律丢弃，不显示别人的运行。
 */
async function hydrate(turn: Turn, mine = generation): Promise<void> {
  const runId = turn.runId
  const sid = sessionID.value
  if (!runId) return
  subscriptions.get(runId)?.abort()
  const controller = new AbortController()
  subscriptions.set(runId, controller)
  const valid = () =>
    withinGeneration(mine) && sessionID.value === sid && !controller.signal.aborted && turns.value.includes(turn)

  const update = (snapshot: RunSnapshot) => {
    if (!valid()) return
    if (snapshot.run_id !== runId) return
    applySnapshot(turn, snapshot)
  }

  try {
    turn.connection = 'connecting'
    const snapshot = await getRun(runId, controller.signal)
    if (!valid()) return
    if (snapshot.session_id !== sid) return
    update(snapshot)
    if (!isTerminalStatus(snapshot.status)) {
      await consumeRun(runId, snapshot, update, controller.signal, (state) => {
        if (valid()) turn.connection = state
      })
    }
    if (!valid()) return
    if (turn.run && isTerminalStatus(turn.run.status)) {
      turn.connection = 'stopped'
      await refreshTurnMetadata(turn)
      void refreshSessions()
    }
  } catch (error) {
    if (!valid()) return
    turn.connection = 'stopped'
    // 运行记录有保留期：历史轮次读不到就只留会话里存的那份回答
    if (error instanceof RunHttpError && (error.status === 404 || error.status === 403)) {
      turn.run = undefined
      turn.runId = undefined
      return
    }
    turn.error = error instanceof Error ? error.message : '运行读取失败'
  } finally {
    if (subscriptions.get(runId) === controller) subscriptions.delete(runId)
  }
}

/** 终态后回读一次会话，把本轮正文与来源换成服务端持久化的那份 */
async function refreshTurnMetadata(turn: Turn): Promise<void> {
  const sid = sessionID.value
  if (!sid) return
  try {
    const payload = await api.session(sid)
    if (sid !== sessionID.value) return
    const index = turns.value.indexOf(turn)
    const match = payload.turns.find((row) => row.run_id && row.run_id === turn.runId)
    if (match?.response && index >= 0) turns.value[index].response = match.response
  } catch {
    /* 元数据只是补充，失败不打断已显示的运行结果 */
  }
}

async function submit(turn: Turn): Promise<void> {
  if (!turn.admission || busy.value) return
  const mine = generation
  const identity = sessionID.value
  busy.value = true
  turn.error = ''
  try {
    let receipt
    try {
      receipt = await createRun(turn.admission.input, turn.admission.key)
    } catch (error) {
      // 只有 5xx/网络错才用同一把 key 立即重试；4xx 是参数或权限问题，重试没意义
      const status = error instanceof RunHttpError ? error.status : 0
      if (status >= 400 && status < 500) throw error
      receipt = await createRun(turn.admission.input, turn.admission.key)
    }
    if (!withinGeneration(mine)) return
    turn.runId = receipt.run_id
    if (!sessionID.value) {
      sessionID.value = receipt.session_id
      sessionTitle.value = turn.question.slice(0, 22)
      try {
        sessionStorage.setItem(sessionKey(), receipt.session_id)
      } catch {
        /* 存储不可用只影响刷新恢复 */
      }
    } else if (sessionID.value !== identity) {
      return
    }
    void refreshSessions()
    // 收下 receipt 就可以放开输入：后续进度走运行卡，不必把输入框一直锁到终态
    void hydrate(turn, mine)
  } catch (error) {
    if (!withinGeneration(mine)) return
    const status = error instanceof RunHttpError ? error.status : 0
    turn.error =
      status === 503
        ? '助手运行能力当前未开启（服务端 v4 开关关闭或服务维护中）'
        : status === 401
          ? '登录已失效，请重新登录'
          : status === 0
            ? '无法连接后端服务，请确认服务已启动'
            : `提交失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    busy.value = false
    await scrollDown()
  }
}

const send = async () => {
  const message = draft.value.trim()
  if (!message || busy.value || loadingSession.value || parsingAttachment.value || searchingAttachments.value) return
  if (attachmentMode.value) {
    const mine = generation
    const result = await attachments.search(message, sessionID.value)
    if (!result || !withinGeneration(mine)) return
    if (!sessionID.value) {
      sessionID.value = result.session_id
      sessionTitle.value = '附件问答'
      try { sessionStorage.setItem(sessionKey(), result.session_id) } catch { /* storage is optional */ }
    }
    if (draft.value.trim() === message) draft.value = ''
    const turn = mapTurn(result.turn, turns.value.length)
    turns.value.push(turn)
    void refreshSessions()
    await scrollDown()
    return
  }
  draft.value = ''
  const turn: Turn = {
    localID: `local-${crypto.randomUUID()}`,
    question: message,
    at: new Date().toISOString(),
    admission: {
      key: crypto.randomUUID(),
      input: sessionID.value ? { message, session_id: sessionID.value } : { message },
    },
  }
  turns.value.push(turn)
  await scrollDown()
  await submit(turns.value[turns.value.length - 1])
}

/** 输入框从 1 行长到 6 行（约 148px）后内部滚动；清空、回填、拼装都要重算 */
const autosize = () => {
  const el = inputter.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 148)}px`
}

/**
 * Enter 发送、Shift+Enter 换行。
 * 中文输入法选词时 Enter 属于组合输入（isComposing），一旦当成发送就会把半截
 * 拼音发出去，所以组合态一律放行给输入法。
 */
const onEnter = (event: KeyboardEvent) => {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  if (busy.value) return
  void send()
}

watch(draft, () => {
  void nextTick(autosize)
})

const stopRun = (turn: Turn) => {
  if (turn.runId) {
    subscriptions.get(turn.runId)?.abort()
    subscriptions.delete(turn.runId)
  }
  turn.connection = 'stopped'
}

const resumeRun = (turn: Turn) => {
  void hydrate(turn, generation)
}

const requestCancel = async (turn: Turn) => {
  if (!turn.runId || turn.cancelling) return
  const ok = await askConfirm({
    title: '请求取消任务',
    body: '取消后以服务器状态为准；本次运行里已经发生的动作不会自动撤销。',
    confirmText: '请求取消',
    danger: true,
  })
  if (!ok) return
  const mine = generation
  const sid = sessionID.value
  turn.cancelling = true
  try {
    const result = await cancelRun(turn.runId)
    if (!withinGeneration(mine) || sessionID.value !== sid || !turns.value.includes(turn)) return
    if (result.session_id !== sid) throw new Error('取消状态绑定不一致，请重新核查')
    if (turn.run && String(result.turn_id) !== String(turn.run.turn_id)) throw new Error('取消状态绑定不一致，请重新核查')
    turn.run = result
    await hydrate(turn, mine)
  } catch (error) {
    if (withinGeneration(mine)) turn.error = error instanceof Error ? error.message : '取消请求未确认，请重试核查'
  } finally {
    turn.cancelling = false
  }
}

/* ---------------- 来源阅读 ---------------- */

const openSource = async (item: RAGSource) => {
  sourceRequest?.abort()
  const controller = new AbortController()
  sourceRequest = controller
  sourceDoc.value = null
  sourceError.value = ''
  sourceLoading.value = true
  try {
    const document = await api.ragKnowledge(item, controller.signal)
    if (sourceRequest === controller) sourceDoc.value = document
  } catch (error) {
    if (sourceRequest !== controller || controller.signal.aborted) return
    sourceError.value = `章节读取失败：${error instanceof Error ? error.message : String(error)}`
    toast('bad', sourceError.value)
  } finally {
    if (sourceRequest === controller) {
      sourceRequest = null
      sourceLoading.value = false
    }
  }
}

/* ---------------- 生命周期 ---------------- */

const restore = async () => {
  let saved = ''
  try {
    saved = sessionStorage.getItem(sessionKey()) || ''
  } catch {
    saved = ''
  }
  if (!saved) return
  const found = sessions.value.find((item) => item.id === saved)
  if (found) await openSession(found)
  else await openSession({ id: saved, title: '历史会话', created_at: '', updated_at: '' })
}

onMounted(async () => {
  await nextTick(() => inputter.value?.focus())
  await refreshSessions()
  await restore()
})

onBeforeUnmount(() => {
  stopAll()
  attachments.reset()
  closeSource()
})
</script>

<template>
  <div class="assistant-grid">
    <section class="chat-card" aria-labelledby="conversation-title">
      <header class="chat-head">
        <div class="chat-title-box">
          <h1 id="conversation-title" class="chat-title" :title="sessionTitle">{{ sessionTitle }}</h1>
          <span v-if="streaming" class="chat-sub" role="status">正在处理</span>
        </div>
        <div class="chat-actions">
          <button class="btn btn-sm" @click="recordsOpen = true"><Icon name="layers" :size="15" />任务记录</button>
          <button class="btn btn-sm" @click="newChat"><Icon name="plus" :size="15" />新会话</button>
        </div>
      </header>

      <div ref="scroller" class="thread">
        <div v-if="!turns.length && !busy" class="welcome">
          <span class="welcome-icon"><Icon name="sparkles" :size="22" /></span>
          <h2>想了解哪方面的运行情况？</h2>
          <div class="welcome-sugs">
            <button v-for="item in suggestions" :key="item" @click="useSuggestion(item)">
              {{ item }}<Icon name="arrowUpRight" :size="13" />
            </button>
          </div>
        </div>

        <article v-for="turn in turns" :key="turn.localID" class="turn">
          <div class="msg is-user">
            <div class="msg-body">
              <div class="bubble">{{ turn.question }}</div>
              <div class="meta"><span class="num">{{ clock(turn.at) }}</span></div>
            </div>
          </div>

          <div class="msg is-agent">
            <div class="msg-body">
              <div v-if="!turn.run" class="speaker"><Icon name="sparkles" :size="13" />运维助手</div>
              <RunAnswer
                v-if="turn.run"
                conversation
                :run="turn.run"
                :connection="turn.connection"
                :error="turn.error"
                :cancelling="turn.cancelling"
                :sources="turn.response?.sources"
                :fallback-answer="turn.response?.answer_markdown"
                @stop="stopRun(turn)"
                @resume="resumeRun(turn)"
                @cancel="requestCancel(turn)"
                @updated="applySnapshot(turn, $event)"
                @source="openSource"
              />

              <div v-else-if="turn.attachmentResult" class="bubble attachment-answer">
                <strong>站内附件检索</strong>
                <p v-if="!turn.attachmentResult.excerpts.length">没有在所选附件中找到相关片段。</p>
                <article v-for="(excerpt, index) in turn.attachmentResult.excerpts" :key="index">
                  <header>{{ excerpt.name }} · {{ excerpt.location }}</header>
                  <p>{{ excerpt.text }}</p>
                </article>
              </div>

              <template v-else-if="turn.response?.answer_markdown">
                <div class="bubble answer" v-html="renderAnswer(turn.response.answer_markdown)"></div>
                <details v-if="turn.response.sources?.length" class="static-sources">
                  <summary>参考来源 · {{ turn.response.sources.length }}</summary>
                  <SourceList :sources="turn.response.sources" @source="openSource" />
                </details>
              </template>

              <p v-else-if="turn.error" class="turn-error">{{ turn.error }}</p>
              <div v-else class="bubble typing"><i></i><i></i><i></i></div>

              <button v-if="turn.run && isTerminalStatus(turn.run.status)" class="retry" @click="useSuggestion(turn.question)">
                <Icon name="refresh" :size="12" />再问一次
              </button>
            </div>
          </div>
        </article>
      </div>

      <div class="composer">
        <input ref="attachmentPicker" type="file" accept=".pdf,.docx,.xlsx,.csv,.txt,.log" multiple hidden @change="attachments.choose" />
        <button type="button" class="icon-btn" title="添加附件" aria-label="添加附件"
          :disabled="busy || loadingSession || !!parsingAttachment || searchingAttachments" @click="attachmentPicker?.click()">
          <Icon name="plus" :size="18" />
        </button>
        <textarea
          ref="inputter"
          v-model="draft"
          rows="1"
          aria-label="向助手提问"
          :placeholder="attachmentMode ? '在所选附件里查找…' : '问问场站、设备或告警…'"
          @keydown.enter="onEnter"
        ></textarea>
        <button v-if="parsingAttachment || searchingAttachments" type="button" class="btn btn-sm" @click="attachments.cancel">取消</button>
        <button v-else class="send-btn" aria-label="发送" :disabled="busy || loadingSession || !draft.trim()" title="发送" @click="send">
          <Icon name="send" :size="16" />
        </button>
      </div>
      <div v-if="attachmentFiles.length || attachmentError || parsingAttachment || searchingAttachments" class="attachment-strip">
        <button v-for="file in attachmentFiles" :key="file.id" type="button" class="btn btn-sm"
          :aria-pressed="chosenAttachments.includes(file.id)" :disabled="!!parsingAttachment || searchingAttachments"
          :title="chosenAttachments.includes(file.id) ? '从本次提问移除' : '在本次提问使用'"
          @click="attachments.toggle(file.id)">{{ chosenAttachments.includes(file.id) ? '移除' : '添加' }} {{ file.name }}</button>
        <p v-if="parsingAttachment" role="status">正在解析 {{ parsingAttachment }}</p>
        <p v-else-if="searchingAttachments" role="status">正在检索所选附件…</p>
        <p v-if="attachmentError" role="alert" class="turn-error">{{ attachmentError }}</p>
      </div>
    </section>

    <aside class="side">
      <section class="session-card">
        <div class="side-head">
          <h2>历史会话</h2>
          <span class="side-count num">{{ historyCount }}</span>
          <button class="icon-btn" title="刷新会话列表" :disabled="loadingList" @click="refreshSessions">
            <Icon name="refresh" :size="14" />
          </button>
        </div>
        <div class="history-tabs" aria-label="会话分类">
          <button :class="{ active: !archivedList }" :aria-pressed="!archivedList" :disabled="sessionWriting" @click="showSessionList(false)">最近</button>
          <button :class="{ active: archivedList }" :aria-pressed="archivedList" :disabled="sessionWriting" @click="showSessionList(true)">已归档</button>
        </div>
        <div v-if="listError" class="side-error">{{ listError }}</div>
        <div class="session-list">
          <div v-for="item in sessions" :key="item.id" class="session-item"
            :class="{ 'is-on': sessionID === item.id }">
            <button class="session-open" :disabled="sessionWriting" :title="item.title || '未命名会话'" @click="openSession(item)">
              <span class="session-main">
                <span class="session-title">{{ item.title || '未命名会话' }}</span>
                <span class="session-meta num">{{ across(item.updated_at) }}更新</span>
              </span>
            </button>
            <div class="session-actions">
              <button class="session-action" :disabled="sessionWriting || loadingList" :title="archivedList ? '恢复会话' : '归档会话'" :aria-label="archivedList ? '恢复会话' : '归档会话'" @click="archiveSession(item)">
                <Icon :name="archivedList ? 'restore' : 'archive'" :size="15" />
              </button>
              <button class="session-action session-del" :disabled="sessionWriting || loadingList" title="删除会话" aria-label="删除会话" @click="removeSession(item)">
                <Icon name="trash" :size="15" />
              </button>
            </div>
          </div>
          <p v-if="loadingList" class="side-empty" role="status">正在加载…</p>
          <p v-else-if="!sessions.length && !listError" class="side-empty">{{ archivedList ? '暂无归档会话' : '暂无历史会话' }}</p>
        </div>
      </section>

      <!-- 示例问题只在空态对话区出现；有消息后右栏不再重复一次 -->
      <section class="limits-card">
        <div class="side-head"><h2>能力边界</h2></div>
        <ul class="limits">
          <li><Icon name="lock" :size="15" /><span>受控动作须本人当场批准，批准只消费一次</span></li>
          <li><Icon name="shield" :size="15" /><span>取消运行不等于撤销已发生的修改</span></li>
          <li><Icon name="database" :size="15" /><span>结论以服务器权威状态为准，结果不明先对账</span></li>
        </ul>
      </section>
    </aside>
  </div>

  <TaskRecordsPanel
    v-model:open="recordsOpen"
    @open-session="openSessionById"
    @refresh-notice="refreshSessions"
  />

  <Drawer
    v-if="sourceDoc || sourceLoading"
    :title="sourceDoc?.heading || '正在读取章节'"
    :sub="sourceDoc ? `${sourceDoc.document} · 版本 ${sourceDoc.version}` : '正在读取参考来源'"
    @close="closeSource"
  >
    <p v-if="sourceLoading" class="loading">读取中…</p>
    <p v-else-if="sourceDoc" class="chapter">{{ sourceDoc.content }}</p>
    <p v-if="sourceError" class="error-box">{{ sourceError }}</p>
  </Drawer>
</template>

<style scoped>
.attachment-strip { display: flex; gap: 6px; flex-wrap: wrap; max-height: 112px; overflow-y: auto; padding: 6px 12px 10px; }
.attachment-strip .btn { max-width: 100%; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attachment-strip .btn[aria-pressed='true'] { background: var(--brand-soft); color: var(--brand-deep); }
.attachment-strip p { flex-basis: 100%; white-space: pre-wrap; overflow-wrap: anywhere; }
.attachment-answer { min-width: 0; }
.attachment-answer article { padding-top: 10px; margin-top: 10px; border-top: 1px solid var(--line); }
.attachment-answer article header { font-size: 12px; color: var(--ink-3); overflow-wrap: anywhere; }
.attachment-answer p { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.assistant-grid {
  --chat-gutter: clamp(16px, 3vw, 48px);
  display: grid;
  grid-template-columns: minmax(0, 1fr) clamp(270px, 23%, 330px);
  height: calc((100dvh - 128px) / var(--content-scale, 1));
  min-height: 430px;
  overflow: hidden;
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 14px;
}

.chat-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.chat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 16px var(--chat-gutter);
  border-bottom: 1px solid var(--line-soft);
}

.chat-title-box { flex: 1 1 160px; min-width: 0; }
.chat-title {
  margin: 0;
  color: var(--ink);
  font-size: 17px;
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-sub { font-size: 12px; color: var(--ink-3); }
.chat-actions { display: flex; gap: 6px; }

.thread {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px var(--chat-gutter);
  display: flex;
  flex-direction: column;
  gap: 32px;
}

.turn {
  flex: none;
  width: 100%;
  max-width: 860px;
  margin-inline: auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.msg { min-width: 0; }
.msg.is-user { align-self: flex-end; max-width: 85%; }
.msg.is-agent { width: 100%; max-width: 48rem; }
.msg.is-user .meta { margin-top: 4px; font-size: 11px; color: var(--ink-4); text-align: end; }
.msg.is-user .bubble {
  background: var(--card-soft);
  border: 1px solid var(--line);
  border-radius: 12px;
  color: var(--ink);
  padding: 9px 14px;
  font-size: 15px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.msg :deep(.speaker) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  background: var(--card-soft);
  border-radius: var(--r-sm);
  color: var(--ink-2);
  font-size: 12px;
  font-weight: 600;
}
.bubble.answer { margin-top: 8px; font-size: 15px; line-height: 1.85; overflow-wrap: anywhere; overflow-x: auto; }
.bubble.answer :deep(p) { margin-bottom: 8px; }
.bubble.answer :deep(ol), .bubble.answer :deep(ul) { padding-inline-start: 1.4em; }
.static-sources { margin-top: 10px; font-size: 12px; }
.static-sources summary { cursor: pointer; }
.welcome {
  margin: auto;
  width: min(100%, 34rem);
  display: grid;
  justify-items: start;
  gap: 14px;
}
.welcome-icon { color: var(--brand); }
.welcome h2 { color: var(--ink); font-size: clamp(18px, 2vw, 24px); font-weight: 600; }
.welcome-sugs { display: grid; width: 100%; }
.welcome-sugs button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 11px 2px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  font-size: 14px;
  color: var(--ink-2);
}
.welcome-sugs button:hover { color: var(--brand); }
.retry {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  margin-top: 6px;
  font-size: 12px;
  color: var(--ink-3);
}
.retry:hover {
  text-decoration: underline;
}

.turn-error {
  font-size: 12px;
  line-height: 1.7;
  color: var(--bad-deep);
}

.typing {
  display: inline-flex;
  gap: 4px;
  padding: 13px;
}

.typing i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--ink-4);
  animation: blink 1.1s infinite alternate;
}

.typing i:nth-child(2) {
  animation-delay: 0.2s;
}

.typing i:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes blink {
  from {
    opacity: 0.25;
  }
}

/* ---------- 输入区 ---------- */

.composer {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  width: calc(100% - 2 * var(--chat-gutter));
  max-width: 860px;
  margin: 0 auto 16px;
  padding: 12px 14px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: var(--card-soft);
}
.composer:focus-within { border-color: var(--brand-line); }

/* 多行输入：1 行起步，长到 6 行后内部滚动，不把对话区继续压扁 */
.composer textarea {
  flex: 1;
  min-width: 0;
  min-height: 2.8em;
  max-height: 148px;
  border: 0;
  outline: none;
  background: transparent;
  font-family: inherit;
  font-size: 12.8px;
  line-height: 1.55;
  color: var(--ink);
  resize: none;
  overflow-y: auto;
  padding: 0;
}

.composer textarea::placeholder {
  color: var(--ink-placeholder);
}

.send-btn {
  display: grid;
  place-items: center;
  flex: 0 0 32px;
  height: 32px;
  border-radius: var(--r-md);
  background: var(--ink);
  color: var(--card);
}
.send-btn:disabled { background: var(--track-bg); color: var(--ink-4); cursor: not-allowed; }
.send-btn:hover:not(:disabled) { background: var(--brand); color: white; }

/* ---------- 右栏 ---------- */

/* 右栏与对话卡严格等高：多出来的长度只允许在会话列表内部滚动 */
.side {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border-left: 1px solid var(--line);
  background: var(--card-soft);
  overflow: hidden;
}

.side-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 15px 18px 10px;
}

.side-head h2 {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--ink);
}

.side-head .icon-btn {
  margin-left: 4px;
  width: 26px;
  height: 26px;
}

/* 批量管理：会话卡右上角入口 + 选择模式工具条 */
.side-count {
  font-size: 11px;
  color: var(--ink-3);
  background: var(--card-soft);
  border: 1px solid var(--line);
  border-radius: var(--r-pill);
  padding: 1px 7px;
}

.side-error {
  margin: 0 18px 10px;
  font-size: 11px;
  color: var(--bad-deep);
}

.session-card {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 160px;
}

.session-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 10px 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: var(--r-md);
  text-align: left;
  transition: background 0.14s ease;
}

.session-item:hover {
  background: var(--card-soft);
}

.session-item.is-on {
  background: var(--card);
  box-shadow: inset 2px 0 var(--brand);
}

.session-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.session-title {
  font-size: 12.5px;
  color: var(--ink-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-item.is-on .session-title {
  color: var(--brand-deep);
  font-weight: 600;
}

.session-meta {
  font-size: 10.5px;
  color: var(--ink-4);
}

.history-tabs { display:flex; gap:4px; margin:0 16px 10px; border-bottom:1px solid var(--line); }
.history-tabs button { padding:8px 12px; color:var(--ink-3); border-bottom:2px solid transparent; font-size:12px; }
.history-tabs button.active { color:var(--brand); border-color:var(--brand); }
.session-open { display:flex; align-items:center; gap:8px; flex:1; min-width:0; text-align:left; padding:8px 4px; }
.session-actions { display:flex; gap:2px; flex-shrink:0; }
.session-action { display:grid; place-items:center; width:30px; height:32px; border-radius:var(--r-sm); color:var(--ink-3); }
.session-action:hover { background:var(--brand-soft); color:var(--brand); }
.session-action:focus-visible, .session-open:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }
.session-action:disabled { opacity:.4; cursor:wait; }
.session-del:hover { background:var(--bad-soft); color:var(--bad); }

.side-empty {
  padding: 18px 10px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
  text-align: center;
}

.limits-card {
  border-top: 1px solid var(--line);
  flex: 0 0 auto;
}

.limits {
  list-style: none;
  margin: 0;
  padding: 0 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.limits li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.limits svg {
  color: var(--ink-4);
  flex: 0 0 auto;
  margin-top: 1px;
}

.chapter {
  font-size: 12.8px;
  line-height: 1.85;
  color: var(--ink-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .assistant-grid { grid-template-columns: minmax(0, 1fr); height: auto; }
  .chat-card {
    height: calc((100dvh - 148px - var(--mnav-h) - var(--safe-b)) / var(--content-scale, 1));
    min-height: 360px;
  }
  .side { border-left: 0; border-top: 1px solid var(--line); }
  .session-card { max-height: 300px; min-height: 160px; }
}
</style>
