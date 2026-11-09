<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import Icon from './components/Icon.vue'
import AlarmInsight from './components/AlarmInsight.vue'
import ModelPanel from './components/ModelPanel.vue'
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
  isAdmin,
  roleLabel,
} from './lib/auth'
import { resetModelBoard, startModelPolling, topbarBalance, topbarTone } from './lib/model-usage'
import { theme, toggleTheme } from './lib/theme'
import { icpNotice } from './lib/site'
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
  { path: '/model-quality', icon: 'cpu', label: '模型' },
  { path: '/reports', icon: 'layers', label: '报表' },
  { path: '/schedules', icon: 'clock', label: '任务' },
  { path: '/assistant', icon: 'assistant', label: '助手' },
]

const pageLabel = computed(() => (route.meta.label as string) || '总览')

/**
 * 手机端底栏只放日常四件事，其余进「更多」面板。
 * 九枚图标平铺在 390px 的屏上每枚只剩 43px，既难点也认不出；按使用频次分两处之后，
 * 底栏每格都有图标 + 文字，桌面端仍走左侧图标轨，互不影响。
 */
const M_PRIMARY = ['/', '/alarms', '/devices', '/assistant']
const mobilePrimary = computed(() => nav.filter((item) => M_PRIMARY.includes(item.path)))
const mobileMore = computed(() => nav.filter((item) => !M_PRIMARY.includes(item.path)))

const isOn = (path: string) => route.path === path || route.path.startsWith(`${path}/`)
const moreOn = computed(() => mobileMore.value.some((item) => isOn(item.path)))
const moreOpen = ref(false)

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
/** 模型与额度面板：只有管理员会看到入口，普通账号打开也只会得到 403 */
const modelOpen = ref(false)
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
  moreOpen.value = false
  keyword.value = ''
})

/* ---------- 顶栏告警 ---------- */

const recent = ref<AlarmView[]>([])
/** 活动告警 = 未处理 + 已确认（铃铛角标与弹层标题用这个口径） */
const activeCount = ref(0)
/**
 * 顶栏快捷入口只留「最新告警」：原先并排的待处理 / 紧急未处理 / 离线设备 / 健康未知
 * 长期为 0 且很难有值，占着一整排宽度却几乎不提供信息；这些数字在总览页指标卡与
 * 各自列表页首屏都能看到，不在这里重复。
 */
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
    /* 只拉顶栏真正要展示的两样：活动告警数（铃铛角标）与最新一条告警（快捷入口）。
       原先为四枚计数胶囊各查一次列表，胶囊删掉后这两次请求也一并省掉。 */
    const [summary, alarms] = await Promise.all([api.summary(), fetchAlarmViews(8)])
    activeCount.value = summary.active_alarms
    recent.value = alarms.list.slice(0, 6)
    latestAlarm.value = alarms.list[0] || null
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
  /* 余额接口本身要求 admin，非管理员不必发这一次请求 */
  if (isAdmin.value) startModelPolling()
}

const halt = () => {
  booted = false
  if (timer) {
    window.clearInterval(timer)
    timer = undefined
  }
  closeRealtime()
  /* 余额是账号资产信息，退出时必须清掉，不能留给下一个登录的人 */
  resetModelBoard()
  modelOpen.value = false
  if (unreadTimer) {
    window.clearTimeout(unreadTimer)
    unreadTimer = undefined
  }
  unreadInsights.value = 0
  activeCount.value = 0
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

        <!-- 快捷入口：只留「最新告警」一枚深链。原先并排的待处理 / 紧急未处理 / 离线设备 /
             健康未知四项长期为 0，占满一整排却几乎不提供信息，已移除；这些数字在总览页指标卡
             与各自列表页首屏都能看到，不在顶栏重复。 -->
        <nav class="tb-quick" aria-label="常用入口">
          <RouterLink
            class="q-chip is-latest"
            :class="{ 'is-zero': !latestAlarm }"
            :title="latestAlarm ? '打开最新告警' : '暂无告警'"
            :to="latestAlarm ? { path: '/alarms', query: { id: String(latestAlarm.id) } } : { path: '/alarms' }"
          >
            <i
              class="q-dot"
              :class="latestAlarm ? `is-${latestAlarm.level}` : ''"
              aria-hidden="true"
            ></i>
            <span class="q-label">最新告警</span>
            <b class="q-num q-metric" :class="{ 'is-zero': !latestAlarm }">{{
              latestAlarm ? METRIC_CN[latestAlarm.metric] || latestAlarm.metric : '—'
            }}</b>
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

          <!-- 模型额度：只在管理员侧出现，数字是告警解读通道的供应商余额 -->
          <button
            v-if="isAdmin"
            class="tb-usage"
            :class="`is-${topbarTone}`"
            title="模型额度：点击逐个查看各功能的模型、地址与余额"
            @click="modelOpen = true"
          >
            <Icon name="cpu" :size="15" />
            <span class="num">{{ topbarBalance || '额度 —' }}</span>
          </button>

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

          <!-- 白天/夜间：只切 <html data-theme>，颜色全部由 tokens.css 承担 -->
          <button
            class="tb-icon"
            :title="theme === 'dark' ? '切换到白天模式' : '切换到夜间模式'"
            :aria-label="theme === 'dark' ? '切换到白天模式' : '切换到夜间模式'"
            :aria-pressed="theme === 'dark'"
            @click="toggleTheme"
          >
            <Icon :name="theme === 'dark' ? 'sun' : 'moon'" :size="19" />
          </button>

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
              <RouterLink to="/about" class="user-link" @click="open = null">
                <Icon name="help" :size="15" />
                关于
              </RouterLink>
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

        <!-- 备案信息随内容流：滚到底看得到，往上滚自然滑出视口（助手页与关于页不重复挂） -->
        <footer v-if="icpNotice && route.path !== '/assistant' && route.path !== '/about'" class="site-foot">
          <span v-html="icpNotice.html"></span>
          <span class="site-foot-sep">·</span>
          <RouterLink to="/about">关于</RouterLink>
        </footer>
      </main>
    </div>

    <AlarmInsight v-if="insight" @close="insight = false" @read="refreshUnread" />
    <ModelPanel v-if="modelOpen" @close="modelOpen = false" />

    <!-- 手机端导航：左侧图标轨在 ≤900px 隐藏，改由底栏四格 + 「更多」面板承担。
         fixed 定位不受 .app 的 overflow 影响（祖先没有 transform），所以放在壳层内部。 -->
    <nav class="mnav" aria-label="主导航">
      <RouterLink
        v-for="item in mobilePrimary"
        :key="item.path"
        :to="item.path"
        class="mnav-btn"
        :class="{ 'is-on': isOn(item.path) }"
      >
        <Icon :name="item.icon" :size="20" :stroke="1.9" />
        <span>{{ item.label }}</span>
      </RouterLink>
      <button
        class="mnav-btn"
        :class="{ 'is-on': moreOn || moreOpen }"
        :aria-expanded="moreOpen"
        aria-haspopup="dialog"
        @click="moreOpen = true"
      >
        <Icon name="more" :size="20" :stroke="1.9" />
        <span>更多</span>
      </button>
    </nav>

    <Teleport to="body">
      <template v-if="moreOpen">
        <div class="msheet-scrim" @click="moreOpen = false"></div>
        <div class="msheet" role="dialog" aria-modal="true" aria-label="更多页面">
          <div class="msheet-head">
            <h4>全部页面</h4>
            <button class="icon-btn" title="关闭" @click="moreOpen = false">
              <Icon name="close" :size="16" />
            </button>
          </div>
          <div class="msheet-grid">
            <RouterLink
              v-for="item in mobileMore"
              :key="item.path"
              :to="item.path"
              class="msheet-item"
              :class="{ 'is-on': isOn(item.path) }"
              @click="moreOpen = false"
            >
              <Icon :name="item.icon" :size="20" :stroke="1.9" />
              <span>{{ item.label }}</span>
            </RouterLink>
            <a href="/demo/" class="msheet-item">
              <Icon name="bolt" :size="20" :stroke="1.9" />
              <span>演示台</span>
            </a>
            <RouterLink to="/about" class="msheet-item" @click="moreOpen = false">
              <Icon name="help" :size="20" :stroke="1.9" />
              <span>关于</span>
            </RouterLink>
          </div>
        </div>
      </template>
    </Teleport>
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

/* 模型额度：胶囊要窄，宽屏显示金额、窄屏只留图标（金额在面板里看） */
.tb-usage {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 9px;
  border-radius: var(--r-md);
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--ink-2);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.tb-usage:hover {
  background: var(--card-soft);
  border-color: var(--line-strong);
}

.tb-usage.is-warn {
  color: var(--warn-deep);
  border-color: var(--warn-line);
}

.tb-usage.is-bad {
  color: var(--bad-deep);
  background: var(--bad-soft);
  border-color: var(--bad-line);
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
  background: var(--hover-bg);
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

/* 账号菜单里的「关于」：与退出登录同一行高，但不上色、不加分隔线 */
.user-link {
  display: flex;
  align-items: center;
  gap: 7px;
  width: calc(100% - 12px);
  margin: 4px 6px 0;
  padding: 7px 8px;
  border-radius: var(--r-sm);
  font-size: 12px;
  font-weight: 550;
  color: var(--ink-2);
}

.user-link:hover {
  background: var(--hover-bg);
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
  background: var(--dot-muted);
}

.tb-quick {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
}

/*
 * 快捷入口统一三件套：状态点 + 标签 + 数值。
 * 底色一律白、边线一律 --line；只有真出现数字时状态点才上色，
 * 全零时整排是安静的灰，顶栏不会变成一排彩色糖豆。
 */
.q-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 8px;
  border-radius: var(--r-lg);
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--ink-4);
  font-size: 11.5px;
  line-height: 1;
  white-space: nowrap;
  transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}

.q-chip:hover {
  background: var(--card-soft);
  border-color: var(--line-strong);
  color: var(--ink-2);
}

.q-chip:focus-visible {
  outline: none;
  border-color: var(--brand-line);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

.q-dot {
  width: 5px;
  height: 5px;
  flex: 0 0 5px;
  border-radius: 50%;
  /* 无数据时的「哑点」：形状固定，颜色才是信息 */
  background: var(--dot-faint);
  transition: background 0.16s ease, box-shadow 0.16s ease;
}

.q-dot.is-live {
  background: var(--brand);
}

.q-dot.is-urgent {
  background: var(--bad);
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.12);
}

.q-dot.is-major {
  background: var(--warn);
}

.q-dot.is-minor {
  background: var(--dot-muted);
}

.q-dot.is-offline {
  background: var(--warn);
}

.q-dot.is-unknown {
  background: var(--violet);
}

.q-num {
  /* 定宽右对齐：0 与 13 切换时整枚胶囊不横向抖动 */
  min-width: 1.1em;
  text-align: right;
  font-size: 12.5px;
  font-weight: 650;
  color: var(--ink);
}

.q-num.is-zero {
  color: var(--text-dim);
  font-weight: 550;
}

.q-chip.is-latest .q-num {
  max-width: 5.5em;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 550;
  color: var(--ink-3);
}

/* 紧急未处理有数字才整枚转红：全屏只允许这一个元素喊话 */
.q-chip.is-urgent {
  background: var(--bad-soft);
  border-color: var(--bad-line-soft);
}

.q-chip.is-urgent .q-label,
.q-chip.is-urgent .q-num {
  color: var(--bad-deep);
}

.q-chip.is-urgent:hover {
  background: var(--bad-hover-bg);
  border-color: var(--bad-line-strong);
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
  border: 1.5px solid var(--card);
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

  /* 窄屏只留图标：金额会被顶栏其他元素挤掉 */
  .tb-usage span {
    display: none;
  }

  .tb-usage {
    padding: 0 7px;
  }
}

/* 顶栏优先级：页名 > 搜索 > 账号；胶囊从右往左逐枚让位，宁可少一枚也不挤压换行 */
@media (max-width: 1500px) {
  .tb-quick .q-chip:nth-child(5) {
    display: none;
  }
}

@media (max-width: 1340px) {
  .tb-quick .q-chip:nth-child(4) {
    display: none;
  }
}

@media (max-width: 1220px) {
  .tb-quick .q-chip:nth-child(3) {
    display: none;
  }
}

@media (max-width: 1120px) {
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
