<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import PowerChart from '../components/PowerChart.vue'
import DonutGauge from '../components/DonutGauge.vue'
import CardMenu from '../components/CardMenu.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import type { Alarm, AvailabilityReport, Device, StationSummary, Summary } from '../lib/api'
import { shell } from '../lib/store'
import { n, pct, stamp } from '../lib/format'
import { useLatestRequest } from '../lib/useLatestRequest'

const router = useRouter()

const metrics = useLatestRequest<Summary>()
const stationRequest = useLatestRequest<StationSummary[]>()
const quality = useLatestRequest<AvailabilityReport>()
const alarmRequest = useLatestRequest<Alarm[]>()
const chart = useLatestRequest<{ points: [number, number][]; note: string }>()
const { data: summary, pending: summaryLoading, error: summaryError } = metrics
const { data: availability, pending: qualityLoading, error: qualityError } = quality
const { pending: stationsLoading, error: stationsError } = stationRequest
const { pending: alarmsLoading, error: alarmsError } = alarmRequest
const { pending: seriesLoading, error: seriesError } = chart
const stations = computed(() => stationRequest.data.value ?? [])
const alarms = computed(() => alarmRequest.data.value ?? [])
const series = computed(() => chart.data.value?.points ?? [])
const seriesNote = computed(() => chart.data.value?.note ?? '')
const sampled = ref<Device[]>([])
const range = ref(24)
const busy = computed(() => summaryLoading.value || stationsLoading.value || qualityLoading.value || alarmsLoading.value || seriesLoading.value)

const ranges = [
  { hours: 6, label: '近 6 小时' },
  { hours: 24, label: '近 24 小时' },
  { hours: 48, label: '近 48 小时' },
]

/**
 * 曲线不取自虚构的“全站聚合”接口：真实后端只有 /devices/:id/telemetry。
 * 按 2h 一段分批取基线站 3 台设备的功率历史再逐点相加，与后端 24h / 5000 点上限兼容。
 */
const CHUNK_MS = 2 * 3_600_000

const loadSeries = () => chart.run(async (signal) => {
  const hours = range.value
  if (!sampled.value.length) {
    const payload = await api.devices({ pageSize: 3, scope: 'baseline' })
    sampled.value = payload.list
  }
  signal.throwIfAborted()
  if (!sampled.value.length) {
    return { points: [], note: '暂无基线站设备' }
  }
  const end = Date.now()
  const start = end - hours * 3_600_000
  const maps = sampled.value.map(() => new Map<number, number>())
  const jobs: Promise<void>[] = []
  for (let from = start; from < end; from += CHUNK_MS) {
    const to = Math.min(end, from + CHUNK_MS)
    sampled.value.forEach((device, index) => {
      jobs.push(
        api.history(device.id, 'power', new Date(from), new Date(to), signal).then((payload) => {
          for (const [ts, value] of payload.points) maps[index].set(ts, value)
        }),
      )
    })
  }
  const results = await Promise.allSettled(jobs)
  if (results.every((item) => item.status === 'rejected')) {
    throw new Error('基线站历史遥测暂不可用，请稍后重试')
  }
  const axis = [...maps[0].keys()].sort((a, b) => a - b)
  const points: [number, number][] = axis.map((ts) => [ts, Math.round(maps.reduce((sum, map) => sum + (map.get(ts) || 0), 0) * 10) / 10])
  return { points, note: results.some((item) => item.status === 'rejected') ? '部分历史遥测未取到，当前曲线不完整' : '' }
}, { keepData: true })

const loadAll = () => {
  void metrics.run(() => api.summary(), { keepData: true })
  void stationRequest.run(async () => (await api.overview('all')).stations, { keepData: true })
  void alarmRequest.run(async () => (await api.alarms({ pageSize: 100 })).list, { keepData: true })
  void quality.run((signal) => api.availability({}, signal), { keepData: true })
  void loadSeries()
}

watch(summary, (s) => {
  if (s) {
    shell.deviceTotal = s.device_total
    shell.onlineDevices = s.online
    shell.alarmsOpen = s.active_alarms
  }
})

watch(range, loadSeries)

onMounted(loadAll)

/* ---------------- 指标 ---------------- */

const energy = computed(() => {
  const value = summary.value?.today_energy_kwh
  if (value == null) return { value: '—', unit: 'kWh' }
  return value >= 10_000
    ? { value: n(value / 1000, 1), unit: 'MWh' }
    : { value: n(value, 0), unit: 'kWh' }
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
      to: '',
    },
    {
      key: 'devices',
      icon: 'server',
      tone: 'info',
      label: '设备在线',
      value: s ? n(s.online) : '—',
      unit: s ? `/ ${n(s.device_total)}` : '',
      to: '/devices',
    },
    {
      key: 'energy',
      icon: 'leaf',
      tone: 'ok',
      label: '今日发电量',
      value: energy.value.value,
      unit: energy.value.unit,
      to: '',
    },
    {
      key: 'alarms',
      icon: 'bell',
      tone: 'brand',
      label: '活动告警',
      value: s ? n(s.active_alarms) : '—',
      unit: '条',
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

const alarmTotal = computed(() => alarms.value.length)
const alarmsLoaded = computed(() => alarmRequest.data.value !== null)

/** 顶部按钮只带未处理数量，与跳转后的筛选条件一致。 */
const unhandledCount = computed(() => alarms.value.filter((item) => item.status === 'unhandled').length)

const peak = computed(() => (series.value.length ? Math.max(...series.value.map(([, value]) => value)) : 0))

/* ---------------- 导出 ---------------- */

const exportCsv = () => {
  if (seriesLoading.value || !series.value.length) return
  const header = 'ts,time,power_kw'
  const rows = series.value.map(([ts, value]) => `${ts},${new Date(ts).toISOString()},${value}`)
  const blob = new Blob([`\ufeff${[header, ...rows].join('\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `power-baseline-${range.value}h.csv`
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
          <span class="stat-label">{{ item.label }}</span>
          <span class="stat-value num">
            {{ item.value }}<em>{{ item.unit }}</em>
          </span>
        </component>
      </section>

      <!-- 功率曲线 -->
      <section class="card chart-card">
        <div class="card-head">
          <span class="card-title">功率输出</span>
          <div class="card-tools">
            <select v-model.number="range" class="select" aria-label="选择时间范围">
              <option v-for="item in ranges" :key="item.hours" :value="item.hours">{{ item.label }}</option>
            </select>
            <CardMenu :payload="series" filename="power-series">
              <button class="card-menu-item" @click="exportCsv">导出 CSV</button>
            </CardMenu>
          </div>
        </div>
        <div class="chart-body">
          <p v-if="seriesLoading" class="table-note" role="status">{{ series.length ? '正在更新功率曲线，当前显示上次结果…' : '正在加载功率曲线…' }}</p>
          <p v-if="seriesError" class="error-box" role="alert">{{ seriesError }}{{ series.length ? '，当前显示上次结果' : '' }}</p>
          <PowerChart v-if="series.length || (!seriesLoading && !seriesError)" :points="series" :height="214" unit="kW" />
        </div>
        <div class="chart-foot">
          <span class="legend"><i></i>基线站有功功率</span>
          <span class="foot-note">
            <span class="num">{{ seriesLoading ? '加载中' : seriesError ? '历史遥测未取到' : seriesNote || `峰值 ${n(peak, 1)} kW · ${series.length} 个采样点` }}</span>
            <span class="foot-sep">·</span>仅基线站 3 台，全站出力见上方指标卡
          </span>
        </div>
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
        <p v-if="qualityLoading" class="table-note" role="status">正在统计近 7 天数据质量，其他区域可正常查看{{ availability ? '；下方暂显示上次结果' : '' }}…</p>
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
    </aside>
  </div>
</template>

<style scoped>
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
  align-items: start;
}

.ov-main,
.ov-side {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}

.chart-body {
  padding: 0 12px 2px 2px;
}

.chart-foot {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 2px 18px 15px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.legend {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.legend i {
  width: 14px;
  height: 3px;
  border-radius: 2px;
  background: var(--brand);
}

.foot-note {
  margin-left: auto;
  color: var(--ink-4);
}

.foot-sep {
  margin: 0 6px;
}

/* ---------- 场站表 ---------- */

.station-table {
  table-layout: fixed;
}

.station-table thead th {
  /* 参考稿的列头是一行浅灰底、正文字重的标签，不是加粗标题 */
  height: 34px;
  font-weight: 500;
  color: #4b5566;
  background: #f7f8fb;
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
  color: #b45309;
}

.dot-cell.is-bad,
.dot-cell.is-bad i {
  color: var(--bad-deep);
}

.dot-cell.is-idle,
.dot-cell.is-idle i {
  color: #8d97a8;
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
  border: 1px solid #fde68a;
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

  .chart-foot {
    flex-wrap: wrap;
    gap: 6px;
  }

  .foot-note {
    margin-left: 0;
    width: 100%;
  }
}
</style>
