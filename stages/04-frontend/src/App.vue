<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import Icon from './components/Icon.vue'
import AlarmInsight from './components/AlarmInsight.vue'
import ToastHost from './components/ToastHost.vue'
import AlarmNoticeHost from './components/AlarmNoticeHost.vue'
import { api } from './lib/api'
import { fetchAlarmViews } from './lib/data'
import type { AlarmView } from './lib/data'
import { connectRealtime, closeRealtime, shell } from './lib/store'
import {
  authToken,
  avatarText,
  clearSession,
  currentUser,
  displayName,
  roleLabel,
} from './lib/auth'
import { toast } from './lib/toast'

import { LEVEL_CN, METRIC_CN, across } from './lib/format'

const route = useRoute()
const router = useRouter()

/** 壳层只在“已登录 + 非公开页”时渲染；登录页不套侧栏与顶栏 */
const showShell = computed(() => Boolean(authToken.value) && route.meta.public !== true)

const nav = [
  { path: '/', icon: 'dashboard', label: '总览' },
  { path: '/alarms', icon: 'alert', label: '告警' },
  { path: '/devices', icon: 'server', label: '设备' },
  { path: '/health', icon: 'activity', label: '健康' },
  { path: '/predictions', icon: 'gauge', label: '预测' },
  { path: '/reports', icon: 'layers', label: '报表' },
  { path: '/schedules', icon: 'clock', label: '任务' },
  { path: '/assistant', icon: 'assistant', label: '助手' },
]

const pageLabel = computed(() => (route.meta.label as string) || '总览')

const clockText = computed(() =>
  shell.now.toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }),
)

const keyword = ref('')
const searchInput = ref<HTMLInputElement | null>(null)

const submitSearch = () => {
  const value = keyword.value.trim()
  if (!value) return
  router.push({ path: '/devices', query: { q: value } })
}

/* Ctrl/⌘ + K 聚焦顶栏搜索（按钮上的 kbd 提示去掉了，快捷键保留） */
const onHotkey = (event: KeyboardEvent) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    searchInput.value?.focus()
  }
}

/* ---------- 顶栏弹层 ---------- */

type Pop = 'guide' | 'bell' | 'user' | null
const insight = ref(false)
const open = ref<Pop>(null)
const guideWrap = ref<HTMLElement | null>(null)
const bellWrap = ref<HTMLElement | null>(null)
const userWrap = ref<HTMLElement | null>(null)

const toggle = (name: Exclude<Pop, null>) => {
  open.value = open.value === name ? null : name
}

const onDocClick = (event: MouseEvent) => {
  if (!open.value) return
  const target = event.target as Node
  if (guideWrap.value?.contains(target) || bellWrap.value?.contains(target) || userWrap.value?.contains(target)) return
  open.value = null
}

watch(() => route.path, () => {
  open.value = null
  keyword.value = ''
})

/* ---------- 顶栏告警 ---------- */

const recent = ref<AlarmView[]>([])
/** 活动告警 = 未处理 + 已确认（铃铛角标与弹层标题用这个口径） */
const activeCount = ref(0)
/** 待处理 = 未处理（快捷入口与「处理告警」按钮用这个口径，点进去条数能对上） */
const pendingCount = ref(0)
const urgentCount = ref(0)
const offlineCount = ref(0)
const unknownCount = ref(0)
const latestAlarm = ref<AlarmView | null>(null)
let timer: number | undefined

/**
 * 解读红点 = 「有未读、且仍值得看的模型解读」（后端 attention=true 的口径：未读 + 24 小时内或有未恢复事件）。
 * 它和铃铛的活动告警数不是一回事：告警可能早就有人看过，解读可能还没人读。
 */
const unreadInsights = ref(0)
let unreadTimer: number | undefined

const refreshUnread = async () => {
  if (!authToken.value) return
  try {
    const page = await api.interpretations({ attention: true, pageSize: 1 })
    unreadInsights.value = page.total
  } catch {
    /* 解读接口不可用时保留上一次的值：清成 0 会让红点骗人（以为都读完了） */
  }
}

/** worker 批量落结果时会连推多帧，合并成 1.2 秒一次，别把接口打满 */
const bumpUnread = () => {
  if (unreadTimer) window.clearTimeout(unreadTimer)
  unreadTimer = window.setTimeout(() => {
    unreadTimer = undefined
    void refreshUnread()
  }, 1200)
}

watch(() => shell.insightRevision, bumpUnread)

const loadHeadline = async () => {
  try {
    const [summary, alarms, pending, urgent] = await Promise.all([
      api.summary(),
      fetchAlarmViews(8),
      api.alarms({ status: 'unhandled', pageSize: 1 }),
      api.alarms({ status: 'unhandled', level: 'urgent', pageSize: 1 }),
    ])
    activeCount.value = summary.active_alarms
    recent.value = alarms.list.slice(0, 6)
    latestAlarm.value = alarms.list[0] || null
    pendingCount.value = pending.total
    urgentCount.value = urgent.total
    /* 离线 = 台账总数 − 在线数（含故障），与 /devices?state=offline 的口径一致 */
    offlineCount.value = Math.max(0, summary.device_total - summary.online)
    unknownCount.value = summary.health_unknown_count ?? 0
  } catch {
    /* 顶栏是旁路信息，失败不阻断页面 */
  }
}

const openAlarm = (alarm: AlarmView) => {
  open.value = null
  router.push({ path: '/alarms', query: { id: String(alarm.id) } })
}

watch(() => shell.alarmRevision, () => {
  if (authToken.value) void loadHeadline()
})

/* 顶栏数据与实时链路只在有会话时启动：登录页不拉任何鉴权接口 */
let booted = false

const boot = () => {
  if (booted || !authToken.value) return
  booted = true
  connectRealtime()
  void loadHeadline()
  void refreshUnread()
  timer = window.setInterval(loadHeadline, 60_000)
}

const halt = () => {
  booted = false
  if (timer) {
    window.clearInterval(timer)
    timer = undefined
  }
  closeRealtime()
  if (unreadTimer) {
    window.clearTimeout(unreadTimer)
    unreadTimer = undefined
  }
  unreadInsights.value = 0
  activeCount.value = 0
  pendingCount.value = 0
  urgentCount.value = 0
  offlineCount.value = 0
  unknownCount.value = 0
  recent.value = []
  latestAlarm.value = null
}

const logout = () => {
  open.value = null
  clearSession()
  halt()
  toast('info', '已退出登录')
  void router.replace({ path: '/login' })
}

/* 登录/过期都会改 authToken：一处监听，避免从登录页回来时壳层还是空的 */
watch(authToken, (value) => {
  if (value) boot()
  else halt()
})

onMounted(() => {
  boot()
  document.addEventListener('click', onDocClick)
  window.addEventListener('keydown', onHotkey)
})

onBeforeUnmount(() => {
  halt()
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('keydown', onHotkey)
})
</script>

<template>
  <div v-if="showShell" class="app">
    <aside class="rail">
      <RouterLink to="/" class="rail-brand" title="云边协同运维平台">
        <Icon name="home" :size="23" :stroke="2" />
      </RouterLink>

      <nav class="rail-nav">
        <RouterLink
          v-for="item in nav"
          :key="item.path"
          :to="item.path"
          class="rail-btn"
          :class="{ 'is-on': route.path === item.path || route.path.startsWith(`${item.path}/`) }"
        >
          <Icon :name="item.icon" :size="21" :stroke="2" />
          <span class="rail-tip">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="rail-foot">
        <!-- 演示告警台是独立静态页，走整页跳转而不是路由，避免进 SPA 的历史栈 -->
        <a href="/demo/" class="rail-btn" style="text-decoration: none" title="演示告警台">
          <Icon name="bolt" :size="20" />
          <span class="rail-tip">演示台</span>
        </a>
      </div>
    </aside>

    <div class="shell">
      <header class="topbar">
        <span class="tb-tab">{{ pageLabel }}</span>

        <label class="tb-search">
          <Icon name="search" :size="16" />
          <input
            ref="searchInput"
            v-model="keyword"
            type="text"
            placeholder="搜索设备编码、名称或场站"
            aria-label="搜索设备"
            @keydown.enter="submitSearch"
          />
        </label>

        <!-- 快捷入口：五个常用深链，数字即当前口径（参考稿的顶栏用法） -->
        <nav class="tb-quick" aria-label="常用入口">
          <RouterLink class="q-chip" :to="{ path: '/alarms', query: { status: 'unhandled' } }">
            <span>待处理告警</span><b class="num" :class="{ 'is-zero': !pendingCount }">{{ pendingCount }}</b>
          </RouterLink>
          <RouterLink
            class="q-chip"
            :class="{ 'is-hot': urgentCount > 0 }"
            :to="{ path: '/alarms', query: { status: 'unhandled', level: 'urgent' } }"
          >
            <span>紧急未处理</span><b class="num" :class="{ 'is-zero': !urgentCount }">{{ urgentCount }}</b>
          </RouterLink>
          <RouterLink class="q-chip" :to="{ path: '/devices', query: { state: 'offline' } }">
            <span>离线设备</span><b class="num" :class="{ 'is-zero': !offlineCount }">{{ offlineCount }}</b>
          </RouterLink>
          <RouterLink class="q-chip" :to="{ path: '/health', query: { state: 'unknown' } }">
            <span>健康未知</span><b class="num" :class="{ 'is-zero': !unknownCount }">{{ unknownCount }}</b>
          </RouterLink>
          <RouterLink
            class="q-chip is-latest"
            :class="{ 'is-zero': !latestAlarm }"
            :to="latestAlarm ? { path: '/alarms', query: { id: String(latestAlarm.id) } } : { path: '/alarms' }"
          >
            <span>最新告警</span><b>{{ latestAlarm ? (METRIC_CN[latestAlarm.metric] || latestAlarm.metric) : '—' }}</b>
          </RouterLink>
        </nav>

        <div class="tb-right">
          <span
            class="tb-live"
            :class="{ 'is-off': !shell.live }"
            :title="shell.live ? '遥测已接入' : '遥测已断开'"
            aria-hidden="true"
          >
            <i></i>
          </span>
          <span class="tb-clock num">{{ clockText }}</span>

          <div class="tb-actions">
          <button class="tb-icon" :class="{ 'is-open': insight }" title="告警解读" @click="insight = true">
            <Icon name="sparkles" :size="20" />
            <i v-if="unreadInsights" class="tb-dot"></i>
          </button>

          <div ref="bellWrap" class="pop-wrap is-bell">
            <button class="tb-icon" title="活动告警" :class="{ 'is-open': open === 'bell' }" @click="toggle('bell')">
              <Icon name="bell" :size="20" />
              <span v-if="activeCount" class="tb-badge">{{ activeCount > 99 ? '99+' : activeCount }}</span>
            </button>
            <div v-if="open === 'bell'" class="popover">
              <div class="pop-head">
                <h4>活动告警 {{ activeCount }} 条</h4>
                <RouterLink to="/alarms" class="link" @click="open = null">全部</RouterLink>
              </div>
              <div v-if="recent.length" class="pop-list">
                <div v-for="alarm in recent" :key="alarm.id" class="pop-item" @click="openAlarm(alarm)">
                  <span class="sev" :class="`sev-${alarm.level}`"></span>
                  <div>
                    <div class="t">{{ alarm.device_name || alarm.device_code || `#${alarm.device_id}` }} · {{ LEVEL_CN[alarm.level] }}</div>
                    <div class="m">{{ alarm.device_code }} · {{ METRIC_CN[alarm.metric] || alarm.metric }} · {{ across(alarm.triggered_at) }}</div>
                  </div>
                </div>
              </div>
              <p v-else class="pop-empty">暂无告警</p>
            </div>
          </div>

          <div ref="guideWrap" class="pop-wrap is-guide">
            <button class="tb-icon" title="数据说明" :class="{ 'is-open': open === 'guide' }" @click="toggle('guide')">
              <Icon name="help" :size="20" />
            </button>
            <div v-if="open === 'guide'" class="popover guide-pop">
              <div class="pop-head"><h4>数据说明</h4></div>
              <div class="guide-body">
                <p>页面接本地假后端（127.0.0.1:8080），需先登录；账号任意、密码非空即可。</p>
                <ul>
                  <li>功率曲线按整点日照系数逐设备累加，与顶部「当前出力」同口径。</li>
                  <li>告警、健康分、边节点状态均为演示数据，不代表真实场站。</li>
                  <li>时间统一按北京时间显示，与设备所在时区一致。</li>
                </ul>
              </div>
            </div>
          </div>

          <span class="tb-divider" aria-hidden="true"></span>

          <div ref="userWrap" class="pop-wrap is-user">
            <button
              class="tb-user"
              :class="{ 'is-open': open === 'user' }"
              :title="`当前账号：${displayName}（${roleLabel}）`"
              @click="toggle('user')"
            >
              <span class="tb-avatar">{{ avatarText }}</span>
              <span class="tb-name">{{ displayName }}</span>
              <Icon name="chevronDown" :size="13" />
            </button>

            <div v-if="open === 'user'" class="popover user-pop">
              <div class="pop-head">
                <h4>{{ displayName }}</h4>
                <span class="chip" :class="currentUser?.role === 'admin' ? 'is-brand' : 'is-plain'">
                  {{ roleLabel }}
                </span>
              </div>
              <div class="user-meta">
                <span>账号</span>
                <b>{{ currentUser?.username || '—' }}</b>
              </div>
              <div class="user-meta">
                <span>会话</span>
                <b>JWT · 8 小时有效</b>
              </div>
              <button class="user-out" @click="logout">
                <Icon name="logout" :size="15" />
                退出登录
              </button>
            </div>
          </div>
          </div>
        </div>
      </header>

      <main class="page">
        <div class="page-inner">
          <RouterView />
        </div>
      </main>
    </div>

    <AlarmInsight v-if="insight" @close="insight = false" @read="refreshUnread" />
  </div>

  <!-- 登录页：不套侧栏与顶栏，直接渲染视图 -->
  <RouterView v-else />

  <ToastHost />
  <AlarmNoticeHost v-if="showShell" />
</template>

<style scoped>
.tb-clock {
  font-size: 11.5px;
  color: var(--ink-4);
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.tb-icon.is-open {
  background: var(--brand-soft);
  color: var(--brand);
}

.guide-pop {
  width: 320px;
}

/* ---------- 顶栏账号菜单 ---------- */

.tb-user.is-open {
  background: #f1f3f7;
}

.user-pop {
  width: 236px;
  padding-bottom: 6px;
}

.user-pop .pop-head {
  align-items: center;
  gap: 8px;
}

.user-pop .pop-head h4 {
  margin-right: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 14px;
  font-size: 11.5px;
  color: var(--ink-4);
}

.user-meta b {
  margin-left: auto;
  font-weight: 600;
  color: var(--ink-2);
  font-variant-numeric: tabular-nums;
}

.user-out {
  display: flex;
  align-items: center;
  gap: 7px;
  width: calc(100% - 12px);
  margin: 6px 6px 0;
  padding: 7px 8px;
  border-radius: var(--r-sm);
  font-size: 12px;
  font-weight: 550;
  color: var(--ink-2);
  border-top: 1px solid var(--line-soft);
  border-top-left-radius: 0;
  border-top-right-radius: 0;
}

.user-out:hover {
  background: var(--bad-soft);
  color: var(--bad-deep);
}

.guide-body {
  padding: 12px 14px 14px;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink-3);
}

.guide-body ul {
  margin: 8px 0 0;
  padding-left: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sev {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-top: 5px;
  flex: 0 0 7px;
  background: var(--ink-3);
}

.sev-urgent {
  background: var(--bad);
}

.sev-major {
  background: var(--warn);
}

.sev-minor {
  background: #94a3b8;
}

.tb-quick {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
}

/* 快捷入口：浅灰底小方标，标签在前、数字在后（参考稿的顶栏读法） */
.q-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 9px;
  border-radius: var(--r-md);
  background: #f2f4f8;
  color: var(--ink-3);
  font-size: 11.5px;
  white-space: nowrap;
  transition: background 0.16s ease, color 0.16s ease;
}

.q-chip:hover {
  background: #e8ecf4;
  color: var(--ink);
}

.q-chip b {
  font-size: 12px;
  font-weight: 600;
  color: var(--ink);
}

.q-chip.is-hot b {
  color: var(--bad);
}

.q-chip b.is-zero,
.q-chip.is-zero b {
  color: #8d97a8;
  font-weight: 500;
}

.q-chip.is-latest b {
  font-weight: 500;
  color: var(--ink-2);
}

/* 解读按钮上的未读点：不带数字，避免和铃铛的角标重复 */
.tb-dot {
  position: absolute;
  top: 7px;
  right: 8px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--bad);
  border: 1.5px solid #fff;
}

/* 图标组与账号之间的竖线：参考稿用它把「工具」和「人」分开 */
.tb-divider {
  width: 1px;
  height: 22px;
  margin: 0 6px;
  background: var(--line);
}

@media (max-width: 1180px) {
  .tb-clock {
    display: none;
  }
}

@media (max-width: 1460px) {
  .tb-quick .q-chip:nth-child(5) {
    display: none;
  }
}

@media (max-width: 1300px) {
  .tb-quick .q-chip:nth-child(4) {
    display: none;
  }
}

@media (max-width: 1180px) {
  .tb-quick .q-chip:nth-child(3) {
    display: none;
  }
}

@media (max-width: 1080px) {
  .tb-quick {
    display: none;
  }
}

@media (max-width: 760px) {
  .tb-live {
    display: none;
  }

  /* 手机上让位给搜索与账号，数据说明在助手页也有 */
  .tb-actions .pop-wrap.is-guide {
    display: none;
  }
}
</style>
