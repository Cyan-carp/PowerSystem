<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import Drawer from '../components/Drawer.vue'
import Pager from '../components/Pager.vue'
import StatusChip from '../components/StatusChip.vue'
import { api, alarmWriteError } from '../lib/api'
import type { AlarmStatistics } from '../lib/api'
import { fetchAlarmViews } from '../lib/data'
import type { AlarmView } from '../lib/data'
import type { Alarm } from '../lib/api'
import { shell } from '../lib/store'
import { askConfirm, toast } from '../lib/toast'
import {
  ALARM_STATE_CN, LEVEL_CN, METRIC_CN, METRIC_DIGITS, METRIC_UNIT,
  clock, n, span, stamp, toneOf,
} from '../lib/format'

/** 读数单位与小数位：风险是 0–1 概率，其余按工程量纲。 */
const unitOf = (metric: string) => METRIC_UNIT[metric] ?? ''
const digitsOf = (metric: string) => METRIC_DIGITS[metric] ?? 1

type StatusKey = 'all' | 'active' | 'unhandled' | 'acked' | 'recovered'
type LevelKey = 'all' | 'urgent' | 'major' | 'minor'

const route = useRoute()
const router = useRouter()

const all = ref<AlarmView[]>([])
const loading = ref(true)
const failed = ref('')
const statusFilter = ref<StatusKey>('all')
const levelFilter = ref<LevelKey>('all')
const keyword = ref('')
const page = ref(1)
const pageSize = 8
const selected = ref<AlarmView | null>(null)
/** 写操作共用一把锁：确认、单条归档、批量归档不并发，按钮同时置灰 */
const busy = ref(false)
const picked = ref<number[]>([])

const statusTabs: { key: StatusKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '活动' },
  { key: 'unhandled', label: '未处理' },
  { key: 'acked', label: '已确认' },
  { key: 'recovered', label: '已恢复' },
]

const levelTabs: { key: LevelKey; label: string }[] = [
  { key: 'all', label: '全部级别' },
  { key: 'urgent', label: '紧急' },
  { key: 'major', label: '重要' },
  { key: 'minor', label: '次要' },
]

/** 列表取最近 100 条（后端 page_size 上限），四格统计走 /alarms/statistics 全量聚合 */
const stats = ref<{ level: AlarmStatistics | null; status: AlarmStatistics | null }>({ level: null, status: null })

/**
 * 取数与对齐。
 * - `full`：首屏与手动刷新，显示载入态；
 * - `quiet`：写操作后与实时广播触发，不闪载入态。
 * 取完把勾选集与已打开的详情跟最新列表对齐——归档后的告警不在列表里（后端固定 archived=FALSE），
 * 对应的勾选与抽屉必须一起撤掉，否则会出现「按钮作用于一条已经看不见的告警」。
 */
const load = async (mode: 'full' | 'quiet' = 'full') => {
  if (mode === 'full') loading.value = true
  try {
    const [payload, level, status] = await Promise.all([
      fetchAlarmViews(100),
      api.alarmStatistics('level').catch(() => null),
      api.alarmStatistics('status').catch(() => null),
    ])
    all.value = payload.list
    stats.value = { level, status }
    failed.value = ''
    prune()
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}

const prune = () => {
  const alive = new Set(all.value.map((item) => item.id))
  picked.value = picked.value.filter((id) => alive.has(id))
  if (!selected.value) return
  const id = selected.value.id
  if (!alive.has(id)) {
    selected.value = null
    syncQuery()
    return
  }
  selected.value = all.value.find((item) => item.id === id) ?? null
}

const filtered = computed(() => {
  const key = keyword.value.trim().toLowerCase()
  return all.value.filter((item) => {
    if (statusFilter.value === 'active' && item.status === 'recovered') return false
    if (statusFilter.value !== 'all' && statusFilter.value !== 'active' && item.status !== statusFilter.value) return false
    if (levelFilter.value !== 'all' && item.level !== levelFilter.value) return false
    if (!key) return true
    return [item.device_code, item.device_name, item.station_code, String(item.id)]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(key))
  })
})

const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))

watch(filtered, () => {
  if (page.value > totalPages.value) page.value = totalPages.value
})

const sumGroups = (payload: AlarmStatistics | null) =>
  (payload?.groups ?? []).reduce<Record<string, number>>((acc, row) => {
    acc[row.key] = row.count
    return acc
  }, {})

const counts = computed(() => {
  const byLevel = stats.value.level ? sumGroups(stats.value.level) : null
  const byStatus = stats.value.status ? sumGroups(stats.value.status) : null
  const fallback = all.value
  const count = (source: Record<string, number> | null, key: string, compute: () => number) =>
    source ? source[key] ?? 0 : compute()
  return {
    total: stats.value.level?.total_events ?? fallback.length,
    unhandled: count(byStatus, 'unhandled', () => fallback.filter((item) => item.status === 'unhandled').length),
    urgent: count(byLevel, 'urgent', () => fallback.filter((item) => item.level === 'urgent').length),
    major: count(byLevel, 'major', () => fallback.filter((item) => item.level === 'major').length),
    recovered: count(byStatus, 'recovered', () => fallback.filter((item) => item.status === 'recovered').length),
  }
})

const strip = computed(() => {
  const total = Math.max(1, counts.value.total)
  const share = (value: number) => `${Math.round((value / total) * 100)}%`
  return [
    { key: 'unhandled', label: '未处理', value: counts.value.unhandled, note: '需值班确认', color: 'var(--brand)' },
    { key: 'urgent', label: '紧急', value: counts.value.urgent, note: `占全部 ${share(counts.value.urgent)}`, color: 'var(--bad)' },
    { key: 'major', label: '重要', value: counts.value.major, note: `占全部 ${share(counts.value.major)}`, color: 'var(--warn)' },
    { key: 'recovered', label: '已恢复', value: counts.value.recovered, note: `占全部 ${share(counts.value.recovered)}`, color: 'var(--ok)' },
  ]
})

const duration = (alarm: Alarm) => {
  const end = alarm.recovered_at ? Date.parse(alarm.recovered_at) : Date.now()
  return span(Math.max(0, (end - Date.parse(alarm.triggered_at)) / 1000))
}

const dayOf = (iso: string) =>
  new Date(iso).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit' })

/* ---------------- 选中与地址同步 ---------------- */

const syncQuery = () => {
  const query: Record<string, string> = {}
  if (statusFilter.value !== 'all') query.status = statusFilter.value
  if (levelFilter.value !== 'all') query.level = levelFilter.value
  if (selected.value) query.id = String(selected.value.id)
  router.replace({ path: '/alarms', query })
}

watch([statusFilter, levelFilter], () => {
  page.value = 1
  syncQuery()
})

const open = (alarm: AlarmView) => {
  selected.value = alarm
  syncQuery()
}

const close = () => {
  selected.value = null
  syncQuery()
}

/* ---------------- 勾选与处置 ---------------- */

const canAck = (alarm: AlarmView) => alarm.status === 'unhandled' && !alarm.recovered_at
const ackLabel = (alarm: AlarmView) =>
  canAck(alarm) ? '确认告警' : alarm.status === 'recovered' ? '已恢复' : '已确认'

const pageIds = computed(() => paged.value.map((row) => row.id))

/** 全选只在「本页」范围生效，避免勾到翻页后看不见的行 */
const pickState = computed<'none' | 'some' | 'all'>(() => {
  const ids = pageIds.value
  const hit = ids.filter((id) => picked.value.includes(id)).length
  if (!ids.length || !hit) return 'none'
  return hit === ids.length ? 'all' : 'some'
})

const togglePick = (id: number) => {
  if (busy.value) return
  picked.value = picked.value.includes(id)
    ? picked.value.filter((item) => item !== id)
    : [...picked.value, id]
}

const togglePickAll = () => {
  if (busy.value) return
  const ids = pageIds.value
  picked.value = pickState.value === 'all'
    ? picked.value.filter((id) => !ids.includes(id))
    : [...new Set([...picked.value, ...ids])]
}

const clearPicks = () => {
  picked.value = []
}

/** 确认：后端只接受「未处理且未恢复」，被别人抢先时会回 409，这里顺手把页面拉回真实状态 */
const ack = async (alarm: AlarmView) => {
  if (busy.value || !canAck(alarm)) return
  busy.value = true
  try {
    await api.ackAlarm(alarm.id)
    toast('ok', `告警 #${alarm.id} 已确认`)
    await load('quiet')
  } catch (error) {
    toast('bad', alarmWriteError(error))
    await load('quiet')
  } finally {
    busy.value = false
  }
}

const archiveOne = async (alarm: AlarmView) => {
  if (busy.value) return
  const go = await askConfirm({
    title: `归档告警 #${alarm.id}`,
    body: '归档只打标记，不删除告警事实、时间线与规则快照。归档后该告警不再出现在列表里，目前没有撤销入口。',
    confirmText: '归档',
  })
  if (!go) return
  busy.value = true
  try {
    await api.archiveAlarm(alarm.id)
    toast('ok', `告警 #${alarm.id} 已归档`)
    await load('quiet')
  } catch (error) {
    toast('bad', alarmWriteError(error))
    await load('quiet')
  } finally {
    busy.value = false
  }
}

const archivePicked = async () => {
  if (busy.value || !picked.value.length) return
  const ids = [...picked.value]
  const go = await askConfirm({
    title: `归档 ${ids.length} 条告警`,
    body: '归档只打标记，不删除告警事实与规则快照。归档后这些告警不再出现在列表里，目前没有撤销入口。',
    confirmText: `归档 ${ids.length} 条`,
    danger: true,
  })
  if (!go) return
  busy.value = true
  try {
    const result = await api.archiveAlarms(ids)
    if (result.failed.length) {
      toast('warn', `已归档 ${result.archived} 条，${result.failed.length} 条未找到（可能已被删除）`)
    } else {
      toast('ok', `已归档 ${result.archived} 条告警`)
    }
    picked.value = []
    await load('quiet')
  } catch (error) {
    toast('bad', alarmWriteError(error))
    await load('quiet')
  } finally {
    busy.value = false
  }
}

/* 别的标签页处置后，本页跟着刷新；自己正在写的时候不插手，避免把回包结果冲掉 */
watch(
  () => shell.alarmRevision,
  () => {
    if (busy.value) return
    void load('quiet')
  },
)

onMounted(async () => {
  const query = route.query
  if (typeof query.status === 'string' && ['active', 'unhandled', 'acked', 'recovered'].includes(query.status)) {
    statusFilter.value = query.status as StatusKey
  }
  if (typeof query.level === 'string' && ['urgent', 'major', 'minor'].includes(query.level)) {
    levelFilter.value = query.level as LevelKey
  }
  await load()
  if (typeof query.id === 'string') {
    const found = all.value.find((item) => String(item.id) === query.id)
    if (found) selected.value = found
  }
})

/* ---------------- 详情 ---------------- */

const gauge = computed(() => {
  const alarm = selected.value
  if (!alarm) return { value: 0, threshold: 0 }
  const ceiling = Math.max(alarm.value, alarm.threshold) * 1.25 || 1
  return {
    value: Math.min(100, (alarm.value / ceiling) * 100),
    threshold: Math.min(100, (alarm.threshold / ceiling) * 100),
  }
})

const overflow = computed(() => {
  const alarm = selected.value
  if (!alarm || !alarm.threshold) return '—'
  return `${(((alarm.value - alarm.threshold) / alarm.threshold) * 100).toFixed(1)}%`
})

const timeline = computed(() => {
  const alarm = selected.value
  if (!alarm) return []
  return [
    { key: 'trigger', label: '触发', at: alarm.triggered_at, done: true },
    { key: 'ack', label: '确认', at: alarm.acked_at, done: !!alarm.acked_at },
    { key: 'recover', label: '恢复', at: alarm.recovered_at, done: !!alarm.recovered_at },
  ]
})
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" :disabled="loading" @click="load('full')">
        <Icon name="refresh" :size="15" />刷新
      </button>
    </div>
  </div>

  <div v-if="failed" class="error-box">告警数据未取到：{{ failed }}</div>

  <!-- 概览条 -->
  <section class="card strip">
    <div v-for="item in strip" :key="item.key" class="strip-cell">
      <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
      <span class="strip-value num">{{ item.value }}</span>
      <span class="strip-note">{{ item.note }}</span>
    </div>
  </section>

  <!-- 列表 -->
  <section class="card">
    <div class="card-head">
      <span class="card-title">告警列表</span>
      <span class="card-sub">{{ filtered.length }} 条匹配</span>
      <div class="card-tools">
        <label class="field-search">
          <Icon name="search" :size="14" />
          <input v-model="keyword" type="text" placeholder="设备编码 / 名称 / 编号" />
        </label>
      </div>
    </div>

    <div class="filters">
      <div class="seg">
        <button
          v-for="tab in statusTabs"
          :key="tab.key"
          :class="{ 'is-on': statusFilter === tab.key }"
          @click="statusFilter = tab.key"
        >
          {{ tab.label }}
        </button>
      </div>
      <div class="seg">
        <button
          v-for="tab in levelTabs"
          :key="tab.key"
          :class="{ 'is-on': levelFilter === tab.key }"
          @click="levelFilter = tab.key"
        >
          {{ tab.label }}
        </button>
      </div>
    </div>

    <!-- 勾选后出现的处置条：只对选中的编号生效，不清空筛选也不改变当前页 -->
    <div v-if="picked.length" class="pickbar">
      <span class="pickbar-count">已选 <b class="num">{{ picked.length }}</b> 条</span>
      <span class="pickbar-hint">归档后不再出现在列表</span>
      <span class="grow"></span>
      <button class="btn btn-sm" :disabled="busy" @click="clearPicks">清空选择</button>
      <button class="btn btn-sm btn-primary" :disabled="busy" @click="archivePicked">
        <Icon name="inbox" :size="14" />归档所选
      </button>
    </div>

    <div v-if="loading" class="loading">读取告警…</div>
    <div v-else class="table-wrap">
      <table class="table alarm-table">
        <colgroup>
          <col style="width: 4%" />
          <col style="width: 8%" />
          <col style="width: 30%" />
          <col style="width: 11%" />
          <col style="width: 15%" />
          <col style="width: 11%" />
          <col style="width: 9%" />
          <col style="width: 12%" />
        </colgroup>
        <thead>
          <tr>
            <th class="pick">
              <span
                class="pick-box"
                :class="{ 'is-on': pickState === 'all', 'is-half': pickState === 'some' }"
                role="checkbox"
                :aria-checked="pickState === 'all'"
                aria-label="选择本页全部告警"
                :title="pickState === 'all' ? '取消本页全选' : '选择本页全部'"
                @click="togglePickAll"
              >
                <Icon v-if="pickState === 'all'" name="check" :size="10" />
              </span>
            </th>
            <th>级别</th>
            <th>设备</th>
            <th>指标</th>
            <th class="r">实测 / 阈值</th>
            <th>状态</th>
            <th class="r">持续</th>
            <th>触发时刻</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in paged"
            :key="row.id"
            class="is-click"
            :class="{ 'is-picked': picked.includes(row.id) }"
            @click="open(row)"
          >
            <td class="pick" @click.stop>
              <span
                class="pick-box"
                :class="{ 'is-on': picked.includes(row.id) }"
                role="checkbox"
                :aria-checked="picked.includes(row.id)"
                :aria-label="`选择告警 #${row.id}`"
                @click="togglePick(row.id)"
              >
                <Icon v-if="picked.includes(row.id)" name="check" :size="10" />
              </span>
            </td>
            <td>
              <StatusChip :tone="toneOf(row.level)" :label="LEVEL_CN[row.level]" />
            </td>
            <td>
              <div class="cell-stack">
                <div class="cell-name">{{ row.device_name || row.device_code || `#${row.device_id}` }}</div>
                <div class="cell-note">
                  {{ row.device_code }} · {{ row.station_code }}
                  <span v-if="row.origin === 'simulation'" class="sim">仿真</span>
                </div>
              </div>
            </td>
            <td>{{ METRIC_CN[row.metric] || row.metric }}</td>
            <td class="r num">
              <b class="over">{{ n(row.value, digitsOf(row.metric)) }}</b>
              <span class="muted"> / {{ n(row.threshold, digitsOf(row.metric) > 1 ? 2 : 0) }} {{ unitOf(row.metric) }}</span>
            </td>
            <td>
              <StatusChip :tone="toneOf(row.status)" :label="ALARM_STATE_CN[row.status]" />
            </td>
            <td class="r num">{{ duration(row) }}</td>
            <td class="tight">
              <span class="num">{{ clock(row.triggered_at) }}</span>
              <span class="day">{{ dayOf(row.triggered_at) }}</span>
            </td>
          </tr>
          <tr v-if="!paged.length">
            <td colspan="8" class="empty">当前筛选下没有告警。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pager
      v-model:page="page"
      :total-pages="totalPages"
      :total="filtered.length"
      :page-size="pageSize"
    />
  </section>

  <!-- 详情 -->
  <Drawer
    v-if="selected"
    :title="`告警 #${selected.id}`"
    :sub="`${METRIC_CN[selected.metric] || selected.metric} · ${selected.device_name || selected.device_code || ''}`"
    @close="close"
  >
    <div class="detail-tags">
      <StatusChip :tone="toneOf(selected.level)" :label="LEVEL_CN[selected.level]" />
      <StatusChip :tone="toneOf(selected.status)" :label="ALARM_STATE_CN[selected.status]" />
      <span class="chip is-plain">{{ selected.origin === 'simulation' ? '仿真数据' : '正常采集' }}</span>
    </div>

    <div class="metric-panel">
      <div class="metric-head">
        <span>
          实测值 <b class="num">{{ n(selected.value, digitsOf(selected.metric)) }}</b>
          <span class="metric-unit">{{ unitOf(selected.metric) }}</span>
        </span>
        <span class="muted">
          阈值 <b class="num">{{ n(selected.threshold, digitsOf(selected.metric) > 1 ? 2 : 0) }}</b>
          <span class="metric-unit">{{ unitOf(selected.metric) }}</span>
        </span>
      </div>
      <div class="metric-track">
        <span class="thr" :style="{ left: `${gauge.threshold}%` }"></span>
        <i class="fill" :style="{ width: `${gauge.value}%` }"></i>
      </div>
      <div class="metric-foot">
        <span>超阈值 {{ overflow }}</span>
        <span class="muted">触发后持续 {{ duration(selected) }}</span>
      </div>
    </div>

    <h3 class="block-title">报警上下文</h3>
    <dl class="kv">
      <dt>设备</dt>
      <dd>{{ selected.device_name || '—' }}</dd>
      <dt>设备编码</dt>
      <dd>{{ selected.device_code || `#${selected.device_id}` }}</dd>
      <dt>所属场站</dt>
      <dd>{{ selected.station_code || '—' }}</dd>
      <dt>触发时间</dt>
      <dd class="num">{{ stamp(selected.triggered_at) }}</dd>
      <dt>规则版本</dt>
      <dd class="num">{{ selected.rule_version }}</dd>
      <dt>关闭原因</dt>
      <dd>{{ selected.close_reason || '—' }}</dd>
    </dl>

    <h3 class="block-title">处置时间线</h3>
    <ol class="timeline">
      <li v-for="step in timeline" :key="step.key" :class="{ 'is-done': step.done }">
        <span class="node"></span>
        <div>
          <div class="step-label">{{ step.label }}</div>
          <div class="step-time num">{{ step.at ? stamp(step.at) : '未发生' }}</div>
        </div>
      </li>
    </ol>

    <p class="panel-note">
      确认只对「未处理且未恢复」的告警生效；若已被他人抢先确认，后端返回冲突，页面会自动重新对齐状态。
      归档只打标记，不删除告警事实、时间线与规则快照，归档后不再出现在列表里，也没有撤销入口。
    </p>

    <template #footer>
      <RouterLink class="btn btn-sm" :to="`/devices?device=${selected.device_id}`" @click="close">
        <Icon name="arrowUpRight" :size="14" />查看设备
      </RouterLink>
      <span class="grow"></span>
      <button class="btn btn-sm" @click="close">关闭</button>
      <button class="btn btn-sm" :disabled="busy" @click="archiveOne(selected)">
        <Icon name="inbox" :size="14" />归档
      </button>
      <button
        class="btn btn-sm btn-primary"
        :disabled="busy || !canAck(selected)"
        :title="canAck(selected) ? '标记为已确认' : '仅未处理且未恢复的告警可以确认'"
        @click="ack(selected)"
      >
        <Icon name="check" :size="14" />{{ ackLabel(selected) }}
      </button>
    </template>
  </Drawer>
</template>

<style scoped>
/* ---------- 概览条 ---------- */

.strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.strip-cell {
  padding: 15px 18px 14px;
  border-left: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.strip-cell:first-child {
  border-left: 0;
}

.strip-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--ink-3);
}

.strip-label i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.strip-value {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
  line-height: 1.25;
}

.strip-note {
  font-size: 11px;
  color: var(--ink-4);
}

/* ---------- 筛选 ---------- */

.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 0 18px 14px;
}

.alarm-table {
  table-layout: fixed;
}

.alarm-table td {
  padding-top: 9px;
  padding-bottom: 9px;
  overflow: hidden;
}

.sim {
  margin-left: 6px;
  padding: 0 5px;
  border-radius: var(--r-pill);
  background: var(--violet-soft);
  border: 1px solid #ddd6fe;
  color: #6d28d9;
  font-size: 9.5px;
  line-height: 15px;
  display: inline-block;
}

.over {
  color: var(--bad);
  font-weight: 650;
}

.day {
  margin-left: 7px;
  font-size: 10.5px;
  color: var(--ink-4);
}

/* ---------- 勾选与处置条 ---------- */

.alarm-table th.pick,
.alarm-table td.pick {
  padding-right: 0;
}

.pick-box {
  display: inline-grid;
  place-items: center;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid var(--line-strong);
  background: #fff;
  color: #fff;
  cursor: pointer;
  vertical-align: middle;
}

.pick-box.is-on {
  background: var(--brand);
  border-color: var(--brand);
}

/* 半选：左半品牌色右半留白，和任务记录里的复选框同一写法 */
.pick-box.is-half {
  background: linear-gradient(90deg, var(--brand) 50%, #fff 50%);
  border-color: var(--brand);
}

.pickbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 18px 12px;
  padding: 7px 10px 7px 12px;
  border-radius: var(--r-md);
  background: var(--brand-soft);
  border: 1px solid #d3e2fb;
  font-size: 12px;
  color: var(--brand-deep);
}

.pickbar .grow {
  flex: 1;
}

.pickbar-count b {
  font-size: 13.5px;
}

.pickbar-hint {
  font-size: 11px;
  color: #5c7fae;
}

/* 勾选行的底色压过 hover 的浅灰，否则鼠标一过就看不出选中了 */
.alarm-table tbody tr.is-picked:hover td {
  background: var(--brand-soft);
}

/* ---------- 抽屉 ---------- */

.detail-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.metric-panel {
  margin-top: 16px;
  padding: 14px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
}

.metric-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 12px;
  color: var(--ink-2);
}

.metric-head b {
  font-size: 17px;
  color: var(--ink);
  margin-left: 3px;
}

.metric-unit {
  margin-left: 2px;
  font-size: 11px;
  color: var(--ink-3);
}

.metric-track {
  position: relative;
  height: 9px;
  margin: 12px 0 9px;
  border-radius: var(--r-pill);
  background: #e8edf6;
}

.metric-track .fill {
  display: block;
  height: 100%;
  border-radius: var(--r-pill);
  background: linear-gradient(90deg, #f87171, #ef4444);
}

.metric-track .thr {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 2px;
  border-radius: 2px;
  background: var(--ink-2);
  z-index: 1;
}

.metric-foot {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--bad-deep);
}

.metric-foot .muted {
  color: var(--ink-3);
}

.block-title {
  margin: 20px 0 10px;
  font-size: 12px;
  font-weight: 650;
  color: var(--ink-2);
}

.timeline {
  list-style: none;
  margin: 0;
  padding: 0 0 0 4px;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.timeline li {
  position: relative;
  display: flex;
  gap: 12px;
  padding: 0 0 16px 14px;
  border-left: 1px solid var(--line);
}

.timeline li:last-child {
  padding-bottom: 0;
  border-left-color: transparent;
}

.timeline .node {
  position: absolute;
  left: -4.5px;
  top: 3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #fff;
  border: 2px solid var(--line-strong);
}

.timeline li.is-done .node {
  border-color: var(--brand);
  background: var(--brand);
}

.step-label {
  font-size: 12.5px;
  color: var(--ink-2);
  font-weight: 550;
}

.timeline li.is-done .step-label {
  color: var(--ink);
}

.step-time {
  font-size: 11px;
  color: var(--ink-3);
  margin-top: 1px;
}

@media (max-width: 1100px) {
  .alarm-table {
    min-width: 960px;
  }
}

.table .num.tight,
.alarm-table td:nth-child(6) {
  white-space: nowrap;
}

@media (max-width: 900px) {
  .strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  /* 窄屏先牺牲说明文字，把按钮留给处置动作 */
  .pickbar-hint {
    display: none;
  }

  .strip-cell:nth-child(3) {
    border-left: 0;
    border-top: 1px solid var(--line-soft);
  }

  .strip-cell:nth-child(4) {
    border-top: 1px solid var(--line-soft);
  }
}

@media (max-width: 680px) {
  .alarm-table {
    min-width: 760px;
  }
}
</style>
