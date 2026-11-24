<script setup lang="ts">
import TelemetryLinkPanel from '../components/TelemetryLinkPanel.vue'
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import DonutGauge from '../components/DonutGauge.vue'
import CardMenu from '../components/CardMenu.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import { fetchAlarmViews } from '../lib/data'
import type { AlarmView } from '../lib/data'
import { putSnapshot } from '../lib/report-snapshot'
import { sessionFingerprint } from '../lib/auth'
import type { AvailabilityReport, Overview, Summary } from '../lib/api'
import { shell } from '../lib/store'
import { clock, n, pct, stamp, LEVEL_CN, METRIC_CN } from '../lib/format'
import { useLatestRequest } from '../lib/useLatestRequest'

const router = useRouter()

const metrics = useLatestRequest<Summary>()
const stationRequest = useLatestRequest<Overview>()
const quality = useLatestRequest<AvailabilityReport>()
const alarmRequest = useLatestRequest<AlarmView[]>()
const { data: summary, pending: summaryLoading, error: summaryError } = metrics
const { data: availability, pending: qualityLoading, error: qualityError } = quality
const { pending: stationsLoading, error: stationsError } = stationRequest
const { pending: alarmsLoading, error: alarmsError } = alarmRequest
const stations = computed(() => stationRequest.data.value?.stations ?? [])
const alarms = computed(() => alarmRequest.data.value ?? [])
const busy = computed(() => summaryLoading.value || stationsLoading.value || qualityLoading.value || alarmsLoading.value)

const loadStations = () => stationRequest.run(signal => api.overview('all', '', signal), { keepData: true })

const statusTypes = [
  { key: 'online', label: '在线非故障', color: 'var(--ok)' },
  { key: 'fault', label: '故障', color: 'var(--bad)' },
  { key: 'offline', label: '离线', color: 'var(--warn)' },
  { key: 'unknown', label: '未知', color: 'var(--ink-placeholder)' },
] as const

const businessStates = computed(() => stations.value.filter(row => row.scope === 'business').map(row => ({
  code: row.station_code, name: row.station_name, total: row.registered_device_count,
  // The overview API includes fresh fault reports in its online count.
  online: row.online - row.fault, fault: row.fault, offline: row.offline, unknown: row.telemetry_unknown,
})))
const businessTotal = computed(() => businessStates.value.reduce((total, row) => total + row.total, 0))
const snapshotAt = computed(() => stationRequest.data.value?.computed_at)

const loadAll = () => {
  void metrics.run(() => api.summary(), { keepData: true })
  void loadStations()
  void alarmRequest.run(async () => (await fetchAlarmViews(100)).list, { keepData: true })
  /* 这张数据质量卡就是「最近 7 天 / 全部设备」的可用率报表，与统计报表默认查询同口径。
     顺手把结果留给统计报表，省掉它首次进入时的那一次全量计算（不新增请求）。
     owner 在发请求前取，putSnapshot 回来时会再比一次：期间登出或换人就作废。 */
  const owner = sessionFingerprint()
  void quality.run(async (signal) => {
    const report = await api.availability({}, signal)
    putSnapshot(report, owner)
    return report
  }, { keepData: true })
}

watch(summary, (s) => {
  if (s) {
    shell.deviceTotal = s.device_total
    shell.onlineDevices = s.online
    shell.alarmsOpen = s.active_alarms
  }
})

let stationTimer: ReturnType<typeof setInterval>
onMounted(() => {
  loadAll()
  stationTimer = setInterval(() => {
    if (!document.hidden && !stationsLoading.value) void loadStations()
  }, 30_000)
})
onBeforeUnmount(() => clearInterval(stationTimer))

/* ---------------- 指标 ---------------- */

const energy = computed(() => {
  const value = summary.value?.today_energy_kwh
  if (value == null) return { value: '—', unit: 'kWh' }
  return value >= 10_000
    ? { value: n(value / 1000, 1), unit: 'MWh' }
    : { value: n(value, 0), unit: 'kWh' }
})

/* 日聚合是逐设备重算的，「截至」取当天最旧的一台：用最新那台会把落后设备藏起来。 */
const energyClock = computed(() => {
  const iso = summary.value?.energy_oldest_at
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
})

const stats = computed(() => {
  const s = summary.value
  return [
    {
      key: 'power',
      icon: 'bolt',
      tone: 'brand',
      label: '实时功率',
      value: s ? n(s.current_power_kw, 1) : '—',
      unit: 'kW',
      note: '',
      to: '',
    },
    {
      key: 'devices',
      icon: 'server',
      tone: 'info',
      label: '设备在线',
      value: s ? n(s.online) : '—',
      unit: s ? `/ ${n(s.device_total)}` : '',
      note: '',
      to: '/devices',
    },
    {
      key: 'energy',
      icon: 'leaf',
      tone: 'ok',
      label: '今日发电量',
      value: energy.value.value,
      unit: energy.value.unit,
      note: energyClock.value ? `数据截至 ${energyClock.value}` : '',
      to: '',
    },
    {
      key: 'alarms',
      icon: 'bell',
      tone: 'brand',
      label: '活动告警',
      value: s ? n(s.active_alarms) : '—',
      unit: '条',
      note: '',
      to: '/alarms?status=unhandled',
    },
  ]
})

/* ---------------- 场站 ---------------- */

const stationRows = computed(() =>
  stations.value.map((row) => {
    const util = row.registered_device_count ? (row.online / row.registered_device_count) * 100 : 0
    return {
      ...row,
      util,
      utilTone: util >= 95 ? 't-ok' : util >= 85 ? 't-warn' : 't-bad',
    }
  }),
)

const openStation = (code: string) => router.push({ path: '/devices', query: { station: code } })

/* ---------------- 右栏 ---------------- */

const meanOf = (list: (number | null)[]) => {
  const valid = list.filter((value): value is number => value != null && Number.isFinite(value))
  return valid.length ? (valid.reduce((sum, value) => sum + value, 0) / valid.length) * 100 : 0
}

const availabilityRate = computed(() => meanOf((availability.value?.stations ?? []).map((row) => row.observed_availability)))
const coverageRate = computed(() => meanOf((availability.value?.stations ?? []).map((row) => row.data_coverage)))

const deviceRate = computed(() => {
  const s = summary.value
  return s && s.device_total ? (s.online / s.device_total) * 100 : 0
})

const healthRate = computed(() => summary.value?.health_score_percent ?? 0)

const accessNote = computed(() => {
  const list = stations.value
  if (!list.length) return ''
  const registered = list.reduce((sum, row) => sum + row.registered_device_count, 0)
  const expected = list.reduce((sum, row) => sum + row.expected_device_count, 0)
  return `已接入 ${n(registered)} / 规划 ${n(expected)} 台`
})

const partialNote = computed(() => {
  const partial = stations.value.filter((row) => row.partial)
  if (!partial.length) return ''
  return `${partial.map((row) => row.station_code).join('、')} 存在未注册设备，统计按已注册口径`
})

const alarmGroups = computed(() => {
  const list = alarms.value
  const total = Math.max(1, list.length)
  const make = (key: string, label: string, count: number, color: string, isLevel = true) => ({
    key,
    label,
    count,
    color,
    isLevel,
    share: isLevel ? (count / total) * 100 : null,
  })
  return [
    make('urgent', '紧急', list.filter((item) => item.level === 'urgent').length, 'var(--bad)'),
    make('major', '重要', list.filter((item) => item.level === 'major').length, 'var(--warn)'),
    make('minor', '次要', list.filter((item) => item.level === 'minor').length, '#94a3b8'),
    make('recovered', '已恢复', list.filter((item) => item.status === 'recovered').length, 'var(--ok)', false),
  ]
})

const attentionAlarms = computed(() => {
  const priority: Record<string, number> = { urgent: 0, major: 1, minor: 2 }
  return alarms.value.filter(item => item.status === 'unhandled' && !item.archived)
    .sort((a, b) => (priority[a.level] ?? 3) - (priority[b.level] ?? 3) || Date.parse(a.triggered_at) - Date.parse(b.triggered_at)).slice(0, 2)
})
const alarmTotal = computed(() => alarms.value.length)
const alarmsLoaded = computed(() => alarmRequest.data.value !== null)

/** 顶部按钮只带未处理数量，与跳转后的筛选条件一致。 */
const unhandledCount = computed(() => alarms.value.filter((item) => item.status === 'unhandled').length)

/* ---------------- 导出 ---------------- */

const exportCsv = () => {
  if (!businessStates.value.length) return
  const header = '场站编码,在册设备,在线非故障,故障,离线,未知,统计时间'
  const rows = businessStates.value.map(row => [
    row.code, row.total, row.online, row.fault, row.offline, row.unknown, snapshotAt.value,
  ].map(value => '"' + String(value ?? '').replace(/^[=+@-]/, "'$&").replaceAll('"', '""') + '"').join(','))
  const blob = new Blob([`\ufeff${[header, ...rows].join('\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `station-status-${snapshotAt.value?.slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" @click="loadAll">
        <Icon name="refresh" :size="15" :class="{ spin: busy }" />刷新
      </button>
      <RouterLink class="btn btn-primary" to="/alarms?status=unhandled">
        <Icon name="alert" :size="15" />处理告警
        <span v-if="summary" class="btn-count num">{{ unhandledCount }}</span>
      </RouterLink>
    </div>
  </div>

  <p v-if="summaryError" class="error-box" role="alert">总览指标未取到：{{ summaryError }}。请稍后刷新重试。</p>
  <p v-else-if="summaryLoading" class="table-note" role="status">正在加载总览指标…</p>

  <TelemetryLinkPanel />

  <div class="ov-grid">
    <div class="ov-main">
      <!-- 指标卡：参考稿构图里它只占左栏，右栏从顶部就开始 -->
      <section class="stats">
        <component
          :is="item.to ? 'button' : 'div'"
          v-for="item in stats"
          :key="item.key"
          class="stat card"
          :class="{ 'is-link': !!item.to }"
          @click="item.to && router.push(item.to)"
        >
          <span class="stat-icon" :class="`t-${item.tone}`">
            <Icon :name="item.icon" :size="27" :stroke="1.85" />
          </span>
          <span class="stat-label">{{ item.label }}<em v-if="item.note" class="stat-note">{{ item.note }}</em></span>
          <span class="stat-value num">
            {{ item.value }}<em>{{ item.unit }}</em>
          </span>
        </component>
      </section>

      <section class="card fleet-card" aria-labelledby="fleet-status-title">
        <div class="card-head">
          <h2 id="fleet-status-title" class="card-title">各站设备状态</h2>
          <div class="card-tools">
            <time v-if="snapshotAt" class="fleet-time num" :datetime="snapshotAt" :title="stamp(snapshotAt)">更新于 {{ clock(snapshotAt) }}</time>
            <CardMenu v-if="businessStates.length" :payload="{ computed_at: snapshotAt, stations: businessStates }" filename="station-status">
              <button class="card-menu-item" @click="exportCsv">导出 CSV</button>
            </CardMenu>
          </div>
        </div>
        <p v-if="stationsError" class="error-box" role="alert">{{ stationRequest.data.value ? '更新失败，显示上次快照。' : '设备状态未取到。' }}<button class="btn btn-sm" @click="loadStations">重试</button></p>
        <div v-if="businessStates.length" class="fleet">
          <div class="fleet-scope">业务场站 <b>{{ businessStates.length }}</b> 座 · 在册 <b>{{ n(businessTotal) }}</b> 台</div>
          <div class="fleet-row fleet-head" aria-hidden="true">
            <span>场站</span><span>状态占比</span>
            <span v-for="type in statusTypes" :key="type.key" class="fleet-key"><i :style="{ background: type.color }"></i>{{ type.label }}</span>
          </div>
          <div v-for="row in businessStates" :key="row.code" class="fleet-row">
            <RouterLink class="fleet-name" :to="{ path: '/devices', query: { station: row.code } }">{{ row.name }} <span aria-hidden="true">›</span></RouterLink>
            <div class="fleet-track" aria-hidden="true">
              <i v-for="type in statusTypes" :key="type.key" :style="{ width: row.total ? (100 * row[type.key] / row.total) + '%' : '0', background: type.color }"></i>
            </div>
            <span v-for="type in statusTypes" :key="type.key" class="fleet-count num" :class="{ flagged: row[type.key] > 0 && type.key !== 'online' }">
              <span class="fleet-mobile-label"><i :style="{ background: type.color }"></i>{{ type.label }}</span>{{ n(row[type.key]) }}
            </span>
          </div>
        </div>
        <p v-else-if="!stationsError" class="empty" role="status">{{ stationsLoading ? '正在读取设备状态…' : '暂无业务场站' }}</p>
      </section>

      <!-- 场站对比 -->
      <section class="card">
        <div class="card-head">
          <span class="card-title">场站对比</span>
          <div class="card-tools">
            <CardMenu :payload="stations" filename="station-overview" to="/devices" to-label="查看设备台账">
              <button class="card-menu-item" @click="loadAll">重新载入</button>
            </CardMenu>
          </div>
        </div>
        <div class="table-wrap">
          <table class="table station-table">
            <colgroup>
              <col />
              <col style="width: 138px" />
              <col style="width: 116px" />
              <col style="width: 132px" />
              <col style="width: 104px" />
              <col style="width: 100px" />
            </colgroup>
            <thead>
              <tr>
                <th>场站</th>
                <th>设备（在册/规划）</th>
                <th>在线率</th>
                <th>当前出力</th>
                <th>活动告警</th>
                <th>遥测未知</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in stationRows" :key="row.station_code" class="is-click" @click="openStation(row.station_code)">
                <td>
                  <div class="cell-main">
                    <span class="st-icon"><Icon name="sun" :size="16" :stroke="1.7" /></span>
                    <span class="cell-stack">
                      <span class="cell-name">{{ row.station_name }}</span>
                      <span class="cell-note">{{ row.station_code }} · {{ row.scope === 'baseline' ? '基线站' : '业务站' }}</span>
                    </span>
                  </div>
                </td>
                <td class="num">{{ n(row.registered_device_count) }} / {{ n(row.expected_device_count) }}</td>
                <td>
                  <span class="dot-cell" :class="row.utilTone.replace('t-', 'is-')">
                    <i></i>{{ pct(row.util, 1) }}
                  </span>
                </td>
                <td class="num">{{ n(row.current_power_kw, 1) }}<small> kW</small></td>
                <td>
                  <span class="dot-cell" :class="row.active_alarms > 2 ? 'is-bad' : row.active_alarms ? 'is-warn' : 'is-idle'">
                    <i></i>{{ n(row.active_alarms) }}
                  </span>
                </td>
                <td>
                  <span class="dot-cell" :class="row.telemetry_unknown ? 'is-warn' : 'is-idle'">
                    <i></i>{{ n(row.telemetry_unknown) }}
                  </span>
                </td>
              </tr>
              <tr v-if="!stationRows.length">
                <td colspan="6" class="empty" :role="stationsError ? 'alert' : 'status'">{{ stationsLoading ? '正在加载场站数据…' : stationsError ? `场站数据未取到：${stationsError}` : '暂无场站数据。' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="partialNote" class="table-note">{{ partialNote }}</p>
      </section>
    </div>

    <!-- 右栏 -->
    <aside class="ov-side">
      <section class="card">
        <div class="side-head">
          <h2>数据质量</h2>
          <CardMenu :payload="availability" filename="availability" to="/reports" to-label="查看统计报表" />
        </div>
        <p v-if="qualityLoading" class="table-note" role="status">正在统计近 7 天数据质量{{ availability ? '；下方暂显示上次结果' : '' }}…</p>
        <p v-if="qualityError" class="error-box" role="alert">数据质量未取到：{{ qualityError }}。{{ availability ? '下方显示上次结果。' : '' }}请稍后刷新重试。</p>
        <div v-if="availability" class="donuts">
          <DonutGauge :value="availabilityRate" tone="brand" label="平均可用率" note="近 7 天" />
          <DonutGauge
            :value="coverageRate"
            :tone="coverageRate >= 95 ? 'ok' : 'warn'"
            label="数据覆盖率"
            note="近 7 天"
          />
        </div>
        <p v-if="availability?.statistics_updated_at" class="table-note">统计更新于 {{ stamp(availability.statistics_updated_at) }}{{ availability.aggregation_state === 'warming' ? ' · 汇总预热中' : availability.aggregation_state === 'degraded' ? ' · 已回原始数据计算' : '' }}</p>
        <div v-if="accessNote" class="queue-note">
          <Icon name="inbox" :size="14" />
          <span>{{ accessNote }}</span>
        </div>
      </section>

      <section class="card">
        <div class="side-head">
          <h2>设备状态</h2>
          <CardMenu :payload="summary" filename="device-summary" to="/devices" to-label="查看设备台账" />
        </div>
        <div v-if="summary" class="donuts">
          <DonutGauge
            :value="deviceRate"
            :tone="deviceRate >= 99 ? 'ok' : 'warn'"
            label="设备在线率"
            :note="summary ? `${n(summary.online)} / ${n(summary.device_total)} 台` : ''"
          />
          <DonutGauge :value="healthRate" tone="brand" label="健康度" :note="summary ? `${n(summary.health_valid_count)} 台有效` : ''" />
        </div>
      </section>

      <section class="card">
        <div class="side-head">
          <h2>告警构成</h2>
          <CardMenu :payload="alarmGroups" filename="alarm-groups" to="/alarms" to-label="查看全部告警" />
        </div>
        <p v-if="alarmsLoading" class="table-note" role="status">正在加载告警…</p>
        <p v-else-if="alarmsError" class="error-box" role="alert">告警未取到：{{ alarmsError }}</p>
        <template v-if="alarmsLoaded">
        <div v-if="alarmTotal" class="share">
          <i
            v-for="group in alarmGroups.filter((item) => item.isLevel)"
            :key="group.key"
            :style="{ width: `${group.share}%`, background: group.color }"
          ></i>
        </div>
        <div class="alarm-rows">
          <div
            v-for="group in alarmGroups"
            :key="group.key"
            class="alarm-row"
            :class="{ 'is-divider': !group.isLevel }"
          >
            <span class="dot" :style="{ background: group.color }"></span>
            <span class="label">{{ group.label }}</span>
            <span v-if="group.share !== null" class="muted">{{ pct(group.share, 0) }}</span>
            <span class="count num">{{ group.count }}</span>
          </div>
        </div>
        <div class="side-foot">
          <StatusChip :tone="alarmTotal ? 'warn' : 'ok'" :label="alarmTotal ? `最近 ${alarmTotal} 条` : '暂无告警'" />
        </div>
        </template>
      </section>
      <section class="card attention-card">
        <div class="side-head"><h2>优先关注</h2><RouterLink class="attention-more" to="/alarms?status=unhandled">全部 <Icon name="chevronRight" :size="13" /></RouterLink></div>
        <p v-if="alarmsLoading" class="table-note" role="status">正在加载…</p>
        <p v-else-if="alarmsError" class="error-box">待处理告警暂不可用</p>
        <template v-else-if="alarmsLoaded">
          <div v-if="attentionAlarms.length" class="attention-list">
            <RouterLink v-for="item in attentionAlarms" :key="item.id" class="attention-item" :title="`${LEVEL_CN[item.level]} · ${item.device_name || item.device_code} · ${stamp(item.triggered_at)}`" :to="`/alarms?id=${item.id}`">
              <i class="attention-dot" :class="item.level"></i>
              <b>{{ METRIC_CN[item.metric] || item.metric }}</b>
              <span class="num">{{ item.device_code }}</span>
              <Icon name="chevronRight" :size="13" />
            </RouterLink>
          </div>
          <p v-else class="table-note">最近记录中没有待处理告警</p>
          <p v-if="attentionAlarms.length" class="attention-scope">最近 {{ alarmTotal }} 条中，按级别优先</p>
        </template>
      </section>
    </aside>
  </div>
</template>

<style scoped>
.attention-more { display:flex; align-items:center; color:var(--brand); gap:4px; font-size:12px; }
.attention-list { padding:0 16px; }
.attention-item { display:flex; align-items:center; gap:8px; padding:9px 4px; border-top:1px solid var(--line); border-radius:var(--r-sm); }
.attention-item:hover { background:var(--card-soft); }
.attention-item b { font-weight:500; font-size:12px; }
.attention-item > span { margin-left:auto; font-size:11px; color:var(--ink-3); }
.attention-item > svg { color:var(--ink-3); }
.attention-dot { width:6px; height:6px; border-radius:50%; background:var(--ink-3); }
.attention-dot.urgent { background:var(--bad); }
.attention-dot.major { background:var(--warn); }
.attention-scope { margin:0; padding:6px 20px 12px; color:var(--ink-3); font-size:10px; }

/* ---------- 指标卡 ---------- */

.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 18px;
}

.stat {
  padding: 16px 18px 18px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  text-align: left;
  gap: 0;
  min-width: 0;
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}

.stat.is-link:hover {
  border-color: var(--brand-line);
  box-shadow: var(--shadow-2);
  transform: translateY(-1px);
}

/* 参考稿的指标图标是裸色字形，不套底色方块；蓝色只有一种深蓝 */
.stat-icon {
  display: grid;
  place-items: center;
  margin-bottom: 13px;
  color: var(--brand);
}

.stat-icon.t-info {
  color: var(--brand);
}

.stat-icon.t-ok {
  color: var(--ok);
}

.stat-icon.t-violet {
  color: var(--violet);
}

.stat-icon.t-bad {
  color: var(--bad);
}

.stat-label {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--ink-2);
  letter-spacing: 0.01em;
}

/* 只挂在「今日发电量」上：数据是逐设备重算的，标出截至时刻免得被当成实时值 */
.stat-note {
  margin-left: 6px;
  font-style: normal;
  font-size: 11.5px;
  font-weight: 400;
  color: var(--ink-3);
  letter-spacing: 0;
  white-space: nowrap;
}

.stat-value {
  margin-top: 3px;
  font-size: 31px;
  line-height: 1.15;
  font-weight: 700;
  letter-spacing: -0.03em;
  color: var(--ink);
  white-space: nowrap;
}

.stat-value em {
  font-style: normal;
  font-size: 13.5px;
  font-weight: 500;
  color: var(--ink-3);
  margin-left: 4px;
  letter-spacing: 0;
}

/* ---------- 主网格 ---------- */

.ov-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 334px;
  gap: 18px;
  align-items: stretch;
}

.ov-main,
.ov-side {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}

.ov-main > .card:last-child, .ov-side > .card:last-child { flex:1; }
.ov-main > .card:last-child { display:flex; flex-direction:column; }
.ov-main > .card:last-child .table-wrap { flex:1; }
.ov-main > .card:last-child .table { height:100%; }
.fleet-card .card-title { margin: 0; }
.fleet-time, .fleet-scope { color: var(--ink-3); font-size: 12px; }
.fleet { padding: 0 clamp(14px, 2vw, 24px) 14px; }
.fleet-scope { margin: 0 0 12px; }
.fleet-scope b { color: var(--ink); }
.fleet-row { display: grid; grid-template-columns: minmax(7rem, 1fr) minmax(60px, 2fr) 5rem repeat(3, 3rem); align-items: center; gap: 12px; min-height: 45px; border-top: 1px solid var(--line-soft); }
.fleet-head { min-height: 30px; border: 0; color: var(--ink-3); font-size: 12px; }
.fleet-key, .fleet-mobile-label { display: inline-flex; align-items: center; gap: 5px; }
.fleet-key { justify-content: flex-end; }
.fleet-key i, .fleet-mobile-label i { width: 8px; height: 8px; border-radius: 2px; }
.fleet-name { color: var(--ink); font-weight: 600; overflow-wrap: anywhere; text-decoration: none; }
.fleet-name:hover, .fleet-name:focus-visible { color: var(--brand-deep); text-decoration: underline; }
.fleet-name span { color: var(--ink-3); }
.fleet-track { display: flex; height: 12px; overflow: hidden; border-radius: 3px; background: var(--track-bg); }
.fleet-count { text-align: right; color: var(--ink-2); }
.fleet-count.flagged { color: var(--ink); font-weight: 750; }
.fleet-mobile-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.fleet-card { container-type: inline-size; }
@container (max-width: 620px) {
  .fleet-head { display: none; }
  .fleet-row:not(.fleet-head) { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; padding: 12px 0; }
  .fleet-name { grid-column: 1 / -1; }
  .fleet-track { grid-column: 1 / -1; margin: 2px 0; height: 9px; }
  .fleet-count { text-align: left; }
  .fleet-mobile-label { position: static; width: auto; height: auto; clip-path: none; display: flex; line-height: 1.5; font-size: 11px; font-weight: 400; color: var(--ink-3); }
}

/* ---------- 场站表 ---------- */

.station-table {
  table-layout: fixed;
}

.station-table thead th {
  /* 参考稿的列头是一行浅灰底、正文字重的标签，不是加粗标题 */
  height: 34px;
  font-weight: 500;
  color: var(--ink-3);
  background: var(--thead-bg);
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}

.station-table tbody td {
  padding-top: 11px;
  padding-bottom: 11px;
  overflow: hidden;
  white-space: nowrap;
}

.station-table tbody td small {
  font-size: 10.5px;
  color: var(--ink-3);
  margin-left: 2px;
}

/* 场站图标是裸色字形，没有底色方块 */
.st-icon {
  display: grid;
  place-items: center;
  flex: 0 0 16px;
  color: var(--brand);
}

/* 状态列统一「圆点 + 文字」，用颜色而不是色块表达语义 */
.dot-cell {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--ink-2);
}

.dot-cell i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex: 0 0 6px;
}

.dot-cell.is-ok,
.dot-cell.is-ok i {
  color: var(--ok-deep);
}

.dot-cell.is-warn,
.dot-cell.is-warn i {
  color: var(--warn-deep);
}

.dot-cell.is-bad,
.dot-cell.is-bad i {
  color: var(--bad-deep);
}

.dot-cell.is-idle,
.dot-cell.is-idle i {
  color: var(--ink-placeholder);
}

.table-note {
  padding: 10px 18px 14px;
  font-size: 11.5px;
  color: var(--warn-deep);
  border-top: 1px solid var(--line-soft);
}

/* ---------- 右栏 ---------- */

.side-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px 20px 2px;
}

.side-head h2 {
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--ink);
}

.donuts {
  display: flex;
  align-items: flex-start;
  justify-content: space-around;
  gap: 10px;
  padding: 12px 16px 16px;
}

.queue-note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0 16px 16px;
  padding: 9px 11px;
  border-radius: var(--r-md);
  background: var(--warn-soft);
  border: 1px solid var(--note-line);
  color: var(--warn-deep);
  font-size: 11.5px;
  line-height: 1.5;
}

.share {
  display: flex;
  height: 8px;
  margin: 2px 20px 14px;
  border-radius: 3px;
  overflow: hidden;
  background: var(--line-soft);
}

.alarm-rows {
  display: flex;
  flex-direction: column;
  padding: 0 20px;
}

.alarm-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 0;
  font-size: 12px;
  color: var(--ink-2);
}

.alarm-row .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 8px;
}

.alarm-row .label {
  min-width: 0;
  flex: 1;
}

.alarm-row .count {
  font-weight: 650;
  color: var(--ink);
}

.alarm-row.is-divider {
  margin-top: 6px;
  padding-top: 9px;
  border-top: 1px solid var(--line-soft);
}

.side-foot {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 12px 20px 16px;
}

.side-foot-note {
  font-size: 10.5px;
  color: var(--ink-4);
}

/* ---------- 响应式 ---------- */

@media (max-width: 1280px) {
  .ov-grid {
    grid-template-columns: minmax(0, 1fr) 300px;
  }
}

@media (max-width: 1180px) {
  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .ov-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .ov-side {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  }
}

@media (max-width: 760px) {
  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .stat {
    padding: 14px 15px 13px;
  }

  .stat-value {
    font-size: 24px;
  }


}
</style>
