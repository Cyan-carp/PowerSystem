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
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Drawer from '../components/Drawer.vue'
import Icon from '../components/Icon.vue'
import RunAnswer from '../components/RunAnswer.vue'
import SourceList from '../components/SourceList.vue'
import TaskRecordsPanel from '../components/TaskRecordsPanel.vue'
import { api } from '../lib/api'
import type { AgentSession, AgentTurn, AgentTurnResponse, KnowledgeChapter, RAGSource } from '../lib/api'
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
const inputter = ref<HTMLInputElement | null>(null)
const recordsOpen = ref(false)

/** 读章节抽屉：知识库来源点「阅读章节」后在这里展开全文 */
const sourceDoc = ref<KnowledgeChapter | null>(null)
const sourceLoading = ref(false)
const sourceError = ref('')

const suggestions = [
  '哪个场站今天出力下降最明显？',
  '未处理告警里最久的是哪一条？',
  '哪些设备的健康分低于 80？',
]

const messageCount = computed(() => turns.value.length)
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

const refreshSessions = async () => {
  loadingList.value = true
  try {
    const payload = await api.sessions(0)
    sessions.value = payload.list
    listError.value = ''
  } catch (error) {
    listError.value = String(error)
  } finally {
    loadingList.value = false
  }
}

const stopAll = () => {
  subscriptions.forEach((controller) => controller.abort())
  subscriptions.clear()
}

const newChat = () => {
  stopAll()
  generation += 1
  sessionID.value = null
  sessionTitle.value = '新会话'
  turns.value = []
  draft.value = ''
  sourceDoc.value = null
  try {
    sessionStorage.removeItem(sessionKey())
  } catch {
    /* 存储不可用时只影响刷新恢复 */
  }
  void nextTick(() => inputter.value?.focus())
}

const mapTurn = (turn: AgentTurn, index: number): Turn => ({
  localID: turn.id != null ? `history-${turn.id}` : `history-${index}-${turn.created_at || ''}`,
  question: turn.question,
  at: turn.created_at || new Date().toISOString(),
  response: turn.response || undefined,
  runId: turn.run_id || undefined,
})

const openSession = async (session: AgentSession) => {
  if (loadingSession.value) return
  stopAll()
  generation += 1
  const mine = generation
  loadingSession.value = true
  try {
    const payload = await api.session(session.id)
    if (mine !== generation) return
    sessionID.value = session.id
    sessionTitle.value = session.title || '未命名会话'
    turns.value = payload.turns.map(mapTurn)
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
    if (mine === generation) listError.value = `会话 ${session.id} 读取失败：${String(error)}`
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
  else listError.value = `会话 ${id} 不在最近的历史会话列表中`
}

const removeSession = async (session: AgentSession) => {
  const ok = await askConfirm({
    title: '删除会话',
    body: `将删除「${session.title || '未命名会话'}」及其全部轮次，删除后无法恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await api.deleteSession(session.id)
    sessions.value = sessions.value.filter((item) => item.id !== session.id)
    if (sessionID.value === session.id) newChat()
    toast('ok', '会话已删除')
  } catch (error) {
    listError.value = `删除失败：${String(error)}`
  }
}

/* ---------------- 批量删除（后端只有单条 DELETE，逐条调用） ---------------- */

const selectMode = ref(false)
const selected = ref<string[]>([])
const batchDeleting = ref(false)
const allSelected = computed(() => sessions.value.length > 0 && selected.value.length === sessions.value.length)

const enterSelectMode = () => {
  selectMode.value = true
  selected.value = []
}
const exitSelectMode = () => {
  selectMode.value = false
  selected.value = []
}
const toggleSelect = (id: string) => {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((item) => item !== id)
    : [...selected.value, id]
}
const toggleSelectAll = () => {
  selected.value = allSelected.value ? [] : sessions.value.map((item) => item.id)
}

/** 单条失败不打断其余：一次删几十条时，不该因为一条 404 就整批停下 */
const removeSelected = async () => {
  if (!selected.value.length || batchDeleting.value) return
  const ok = await askConfirm({
    title: '批量删除会话',
    body: `将删除 ${selected.value.length} 段会话，删除后无法恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  const ids = [...selected.value]
  batchDeleting.value = true
  let failed = 0
  try {
    for (const id of ids) {
      try {
        await api.deleteSession(id)
      } catch {
        failed += 1
      }
    }
    if (sessionID.value && ids.includes(sessionID.value)) newChat()
    exitSelectMode()
    await refreshSessions()
    if (failed) listError.value = `${ids.length - failed} 条已删除，${failed} 条失败`
    else toast('ok', `已删除 ${ids.length} 段会话`)
  } finally {
    batchDeleting.value = false
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
  if (!message || busy.value) return
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
  sourceDoc.value = null
  sourceError.value = ''
  sourceLoading.value = true
  try {
    sourceDoc.value = await api.knowledge(item.id)
  } catch (error) {
    sourceError.value = `章节读取失败：${error instanceof Error ? error.message : String(error)}`
    toast('bad', sourceError.value)
  } finally {
    sourceLoading.value = false
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
}

onMounted(async () => {
  await nextTick(() => inputter.value?.focus())
  await refreshSessions()
  await restore()
})

onBeforeUnmount(stopAll)
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" @click="recordsOpen = true">
        <Icon name="layers" :size="15" />任务记录
        <span class="records-hint">本人已受理</span>
      </button>
      <button class="btn btn-primary" @click="newChat">
        <Icon name="plus" :size="15" />新会话
      </button>
    </div>
  </div>

  <div class="assistant-grid">
    <section class="card chat-card">
      <div class="chat-head">
        <span class="assistant-avatar"><Icon name="sparkles" :size="17" /></span>
        <div class="chat-title-box">
          <div class="chat-title">{{ sessionTitle }}</div>
          <div class="chat-sub">
            {{ sessionID ? `会话 ${sessionID}` : '发送首条消息后由服务端建会话' }} · {{ messageCount }} 条消息
            <template v-if="streaming"> · 有运行在进行</template>
          </div>
        </div>
        <span class="chat-chip">assistant/v4-draft1</span>
      </div>

      <div ref="scroller" class="thread">
        <div v-if="!turns.length && !busy" class="welcome">
          <span class="welcome-icon"><Icon name="sparkles" :size="22" /></span>
          <h3>从一条运行记录开始</h3>
          <p>
            发问后服务端先受理一次运行，正文、进度、待批准动作与产物都从同一条事件流里出来。
            运行中途离开页面不会中断任务，回到这段会话会自动重新挂上。
          </p>
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
            <span class="msg-avatar"><Icon name="sparkles" :size="14" /></span>
            <div class="msg-body">
              <RunAnswer
                v-if="turn.run"
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

              <template v-else-if="turn.response?.answer_markdown">
                <div class="bubble answer" v-html="renderAnswer(turn.response.answer_markdown)"></div>
                <details v-if="turn.response.sources?.length" class="static-sources">
                  <summary>参考来源 · {{ turn.response.sources.length }}</summary>
                  <SourceList :sources="turn.response.sources" @source="openSource" />
                </details>
              </template>

              <p v-else-if="turn.error" class="turn-error">{{ turn.error }}</p>
              <div v-else class="bubble typing"><i></i><i></i><i></i></div>

              <div class="meta">
                <span v-if="turn.runId" class="rid num">{{ turn.runId }}</span>
                <button v-if="turn.run && isTerminalStatus(turn.run.status)" class="retry" @click="useSuggestion(turn.question)">
                  再问一次
                </button>
              </div>
            </div>
          </div>
        </article>
      </div>

      <div class="composer">
        <label class="composer-field">
          <Icon name="assistant" :size="16" />
          <input
            ref="inputter"
            v-model="draft"
            type="text"
            placeholder="问场站、设备或告警的情况…"
            @keydown.enter="send"
          />
        </label>
        <button class="send-btn" :disabled="busy || !draft.trim()" title="发送" @click="send">
          <Icon name="send" :size="16" />
        </button>
      </div>
    </section>

    <aside class="side">
      <section class="card session-card">
        <div class="side-head">
          <h2>历史会话</h2>
          <span class="side-count num">{{ historyCount }}</span>
          <button v-if="!selectMode" class="side-manage" :disabled="!sessions.length" title="批量删除会话" @click="enterSelectMode">批量管理</button>
          <button v-if="!selectMode" class="icon-btn" title="刷新会话列表" :disabled="loadingList" @click="refreshSessions">
            <Icon name="refresh" :size="14" />
          </button>
        </div>
        <div v-if="selectMode" class="batch-bar">
          <button class="batch-all" :class="{ 'is-on': allSelected }" @click="toggleSelectAll">
            <span class="session-check" :class="{ 'is-on': allSelected }"><Icon v-if="allSelected" name="check" :size="11" /></span>
            全选
          </button>
          <span class="batch-count num">已选 {{ selected.length }}</span>
          <button class="btn btn-sm batch-del" :disabled="!selected.length || batchDeleting" @click="removeSelected">
            {{ batchDeleting ? '删除中…' : `删除${selected.length ? ` (${selected.length})` : ''}` }}
          </button>
          <button class="btn btn-sm" :disabled="batchDeleting" @click="exitSelectMode">取消</button>
        </div>
        <div v-if="listError" class="side-error">{{ listError }}</div>
        <div class="session-list">
          <button
            v-for="item in sessions"
            :key="item.id"
            class="session-item"
            :class="{ 'is-on': sessionID === item.id && !selectMode, 'is-checked': selectMode && selected.includes(item.id) }"
            @click="selectMode ? toggleSelect(item.id) : openSession(item)"
          >
            <span v-if="selectMode" class="session-check" :class="{ 'is-on': selected.includes(item.id) }">
              <Icon v-if="selected.includes(item.id)" name="check" :size="11" />
            </span>
            <span class="session-main">
              <span class="session-title">{{ item.title || '未命名会话' }}</span>
              <span class="session-meta num">{{ across(item.updated_at) }}更新</span>
            </span>
            <span v-if="!selectMode" class="session-del" title="删除会话" @click.stop="removeSession(item)">
              <Icon name="close" :size="13" />
            </span>
          </button>
          <p v-if="!sessions.length && !loadingList" class="side-empty">还没有历史会话，发一条消息就会出现在这里。</p>
        </div>
        <div class="side-foot">
          <button class="btn btn-sm" @click="newChat"><Icon name="plus" :size="13" />新会话</button>
          <span class="side-foot-note">GET /agent/sessions</span>
        </div>
      </section>

      <!-- 空态已经在对话区给了示例问题，这里等聊起来再出现 -->
      <section v-if="turns.length" class="card">
        <div class="side-head"><h2>试试这样问</h2></div>
        <div class="sugs">
          <button v-for="item in suggestions" :key="item" class="sug" @click="useSuggestion(item)">
            <span>{{ item }}</span>
            <Icon name="arrowUpRight" :size="13" />
          </button>
        </div>
      </section>

      <section class="card">
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
    :sub="sourceDoc ? `${sourceDoc.document} · 版本 ${sourceDoc.version}` : 'GET /agent/knowledge/:id'"
    @close="sourceDoc = null"
  >
    <p v-if="sourceLoading" class="loading">读取中…</p>
    <p v-else-if="sourceDoc" class="chapter">{{ sourceDoc.content }}</p>
    <p v-if="sourceError" class="error-box">{{ sourceError }}</p>
  </Drawer>
</template>

<style scoped>
/* 任务记录入口：主按钮旁的第二入口，副标签说明它是只读记录 */
.records-hint {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: var(--r-pill);
  background: var(--card-soft);
  border: 1px solid var(--line);
  color: var(--ink-4);
  font-size: 10px;
  font-weight: 400;
}

.assistant-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 18px;
  align-items: start;
}

/* ---------- 会话卡 ---------- */

.chat-card {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 186px);
  min-height: 480px;
  overflow: hidden;
}

.chat-head {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line-soft);
}

.assistant-avatar {
  width: 34px;
  height: 34px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  flex: 0 0 34px;
}

.chat-title-box {
  min-width: 0;
}

.chat-title {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 420px;
}

.chat-sub {
  font-size: 11px;
  color: var(--ink-3);
  margin-top: 1px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-chip {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--ink-3);
  border: 1px solid var(--line);
  border-radius: var(--r-pill);
  padding: 2px 8px;
  white-space: nowrap;
}

.thread {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 20px 8px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: linear-gradient(180deg, #fcfdff 0%, #ffffff 100%);
}

/* 一轮 = 一句提问 + 一个回答块；回答块要能撑满宽度放运行卡 */
.turn {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.msg {
  display: flex;
  gap: 9px;
}

.msg.is-user {
  align-self: flex-end;
  flex-direction: row-reverse;
  max-width: 74%;
}

.msg.is-agent {
  align-items: flex-start;
}

.msg.is-agent .msg-body {
  flex: 1;
  min-width: 0;
}

/* ---------- 空会话欢迎态 ---------- */

.welcome {
  margin: auto;
  max-width: 560px;
  padding: 18px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.welcome-icon {
  width: 46px;
  height: 46px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  box-shadow: 0 0 0 7px #eef2fb;
  margin-bottom: 13px;
}

.welcome h3 {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--ink);
}

.welcome p {
  margin-top: 6px;
  font-size: 12.5px;
  line-height: 1.72;
  color: var(--ink-3);
}

.welcome-sugs {
  margin-top: 16px;
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.welcome-sugs button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 13px;
  border-radius: var(--r-md);
  border: 1px solid var(--line);
  background: #fff;
  font-size: 12.5px;
  color: var(--ink-2);
  text-align: left;
  transition: border-color 0.16s ease, background 0.16s ease, color 0.16s ease;
}

.welcome-sugs button:hover {
  border-color: var(--brand-line);
  background: var(--brand-soft);
  color: var(--brand-deep);
}

.welcome-sugs svg {
  color: var(--ink-4);
  flex: 0 0 auto;
}

.msg-avatar {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  flex: 0 0 26px;
  margin-top: 2px;
}

.msg-body {
  min-width: 0;
}

.bubble {
  padding: 10px 13px;
  border-radius: 8px 8px 8px 3px;
  background: var(--card-soft);
  border: 1px solid var(--line);
  font-size: 12.8px;
  line-height: 1.68;
  color: var(--ink-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.msg.is-user .bubble {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
  border-radius: 8px 8px 3px 8px;
}

/* 历史回答是 markdown 渲染出来的，不能继承 pre-wrap（会把段落撑开） */
.bubble.answer {
  white-space: normal;
}

.bubble.answer :deep(p) {
  margin: 0 0 8px;
}

.bubble.answer :deep(p:last-child) {
  margin-bottom: 0;
}

.bubble.answer :deep(strong) {
  color: var(--ink);
}

.static-sources {
  margin-top: 8px;
  font-size: 12px;
}

.static-sources summary {
  cursor: pointer;
  color: var(--ink-3);
  font-size: 11.5px;
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: 10.5px;
  color: var(--ink-4);
}

.meta .rid {
  color: var(--ink-3);
}

.retry {
  font-size: 10.5px;
  color: var(--brand);
  font-weight: 550;
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
  align-items: center;
  gap: 10px;
  padding: 12px 16px 14px;
  border-top: 1px solid var(--line-soft);
}

.composer-field {
  flex: 1;
  min-width: 0;
  height: 40px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 0 13px;
  border-radius: var(--r-md);
  border: 1px solid var(--line-strong);
  background: #fff;
  color: var(--ink-3);
}

.composer-field:focus-within {
  border-color: var(--brand-line);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

.composer-field input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: none;
  background: transparent;
  font-size: 12.8px;
  color: var(--ink);
}

.composer-field input::placeholder {
  color: var(--ink-placeholder);
}

.send-btn {
  width: 40px;
  height: 40px;
  border-radius: var(--r-md);
  display: grid;
  place-items: center;
  background: var(--brand);
  color: #fff;
  transition: background 0.16s ease, opacity 0.16s ease;
}

.send-btn:hover:not(:disabled) {
  background: var(--brand-deep);
}

.send-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* ---------- 右栏 ---------- */

.side {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
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
.side-manage {
  margin-left: auto;
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  background: #fff;
  color: var(--ink-3);
  font-size: 11px;
}

.side-manage:hover:not(:disabled) {
  border-color: var(--line-strong);
  color: var(--ink);
}

.side-manage:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 18px 10px;
  padding: 8px 10px;
  border: 1px solid var(--brand-line);
  border-radius: var(--r-md);
  background: var(--brand-soft);
}

.batch-all {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ink);
  font-size: 11.5px;
}

.batch-count {
  margin-right: auto;
  font-size: 11px;
  color: var(--ink-3);
}

.batch-del {
  color: var(--bad-deep);
  border-color: #f0c7c2;
}

.batch-del:hover:not(:disabled) {
  background: var(--bad-soft);
}

/* 选择态方块：不用原生 checkbox，避免 input 嵌进 button 的结构问题 */
.session-check {
  flex: none;
  display: grid;
  place-items: center;
  width: 15px;
  height: 15px;
  border-radius: 3px;
  border: 1px solid var(--line-strong);
  background: #fff;
  color: #fff;
}

.session-check.is-on {
  background: var(--brand);
  border-color: var(--brand);
}

.session-item.is-checked {
  background: var(--brand-soft);
}

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
  max-height: calc(100vh - 300px);
  min-height: 240px;
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
  padding: 9px 10px;
  border-radius: var(--r-md);
  text-align: left;
  transition: background 0.14s ease;
}

.session-item:hover {
  background: var(--card-soft);
}

.session-item.is-on {
  background: var(--brand-soft);
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

.session-del {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  color: var(--ink-4);
  opacity: 0;
  transition: opacity 0.14s ease, background 0.14s ease, color 0.14s ease;
}

.session-item:hover .session-del {
  opacity: 1;
}

.session-del:hover {
  background: var(--bad-soft);
  color: var(--bad-deep);
}

.side-empty {
  padding: 18px 10px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
  text-align: center;
}

.side-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 18px 14px;
  border-top: 1px solid var(--line-soft);
}

.side-foot-note {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--ink-4);
}

.sugs {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0 14px 16px;
}

.sug {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--r-sm);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  font-size: 12px;
  color: var(--ink-2);
  text-align: left;
}

.sug:hover {
  border-color: var(--brand-line);
  color: var(--brand-deep);
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

@media (max-width: 1100px) {
  .assistant-grid {
    grid-template-columns: minmax(0, 1fr) 280px;
  }

  .chat-card {
    height: calc(100vh - 210px);
  }
}

@media (max-width: 900px) {
  .assistant-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .chat-card {
    height: calc(100vh - 250px);
  }

  .session-card {
    max-height: 320px;
  }
}
</style>
