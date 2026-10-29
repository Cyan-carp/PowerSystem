<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import type { AvailabilityItem, AvailabilityReport, Device, StationRow, TimelinessReport } from '../lib/api'
import { n, pct, stamp } from '../lib/format'
import { toast } from '../lib/toast'

const route = useRoute()
const router = useRouter()

type TabKey = 'availability' | 'timeliness'
const tabs: { key: TabKey; label: string }[] = [
  { key: 'availability', label: '可用率' },
  { key: 'timeliness', label: '告警及时性' },
]

const tab = ref<TabKey>('availability')
const availability = ref<AvailabilityReport | null>(null)
const timeliness = ref<TimelinessReport | null>(null)
const stations = ref<StationRow[]>([])
const loading = ref(true)
const failed = ref('')

/* 报表窗口：前端按北京时间（+08:00）解释输入，与设备详情页保持一致，
   不能用浏览器本地时区，否则非 UTC+8 的机器上窗口会整体挪走。 */
const startText = ref('')
const endText = ref('')
const stationFilter = ref('')
const deviceFilter = ref('')
const deviceOptions = ref<{ id: number; code: string; name: string }[]>([])
const rangeError = ref('')

const toLocalInput = (date: Date) => new Date(date.getTime() + 8 * 3_600_000).toISOString().slice(0, 16)
const parseInput = (text: string) => {
  const value = Date.parse(`${text}:00+08:00`)
  return Number.isFinite(value) ? new Date(value) : null
}

/** 统一校验并转成接口要的 ISO；校验不过就返回 null，不发请求 */
const rangeParams = () => {
  const start = parseInput(startText.value)
  const end = parseInput(endText.value)
  if (!start || !end) { rangeError.value = '请选择完整的开始与结束时间'; return null }
  if (end <= start) { rangeError.value = '结束时间必须晚于开始时间'; return null }
  if (end.getTime() - start.getTime() > 31 * 86_400_000) { rangeError.value = '时间范围不能超过 31 天'; return null }
  rangeError.value = ''
  return { start: start.toISOString(), end: end.toISOString() }
}

const windowLabel = computed(() => {
  const start = parseInput(startText.value)
  const end = parseInput(endText.value)
  return start && end ? `${stamp(start.toISOString())} 至 ${stamp(end.toISOString())}` : '未选择'
})

const deviceQuery = () => ({
  station_code: stationFilter.value || undefined,
  device_id: deviceFilter.value ? Number(deviceFilter.value) : undefined,
})

/** 设备下拉按当前场站过滤；台账不大，一次翻完并缓存 */
const loadDevices = async () => {
  try {
    const all: Device[] = []
    for (let page = 1; ; page += 1) {
      const result = await api.devices({ page, pageSize: 100, stationCode: stationFilter.value || undefined })
      all.push(...result.list)
      if (all.length >= result.total || !result.list.length) break
    }
    deviceOptions.value = all.map(item => ({ id: item.id, code: item.device_code, name: item.name }))
  } catch {
    deviceOptions.value = []
  }
}

const onStationChange = () => {
  deviceFilter.value = ''
  void loadDevices()
}

const load = async () => {
  const range = rangeParams()
  if (!range) return
  loading.value = true
  try {
    const [avail, timely, stationPayload] = await Promise.all([
      api.availability({ ...range, ...deviceQuery() }),
      api.timeliness({ ...range, ...deviceQuery() }),
      api.stations(),
    ])
    availability.value = avail
    timeliness.value = timely
    stations.value = stationPayload.list
    failed.value = ''
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  const end = new Date()
  startText.value = toLocalInput(new Date(end.getTime() - 7 * 86_400_000))
  endText.value = toLocalInput(end)
  void load()
  void loadDevices()
})

const applyQuery = () => {
  const key = route.query.tab as string | undefined
  if (key === 'availability' || key === 'timeliness') tab.value = key
}
applyQuery()
watch(
  () => route.query,
  () => applyQuery(),
)
watch(tab, (value) => router.replace({ query: { tab: value } }))

const stationName = computed(() => {
  const map = new Map<string, string>()
  for (const item of stations.value) map.set(item.station_code, item.station_name)
  return map
})

/* 时长统一「时:分:秒」（00:10:06）。报表要跨行比大小，只有固定位数加
   等宽数字才能竖着扫；原来按量级切「8.5 分 / 1.5 小时」，位数不齐也对不齐。 */
const dur = (ms: number | null | undefined) => {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '—'
  if (ms > 0 && ms < 1000) return '<00:00:01'
  const total = Math.round(ms / 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`
}
/** 报表按设备时长汇总，单位是台·时（设备数 × 小时），不是墙钟时长 */
const dh = (ms: number | null | undefined) => (ms == null ? '—' : n(ms / 3_600_000, 0))

/** 可用率缺失按 0 处理（表格不画空条），文字仍显示「—」由 pct 自己判空 */
const availOf = (row: { observed_availability: number | null }) => row.observed_availability ?? 0
const coverageOf = (row: { data_coverage: number | null }) => row.data_coverage ?? 0

const stationRows = computed(() => {
  const list = (availability.value?.stations ?? []).map((row) => ({
    ...row,
    name: stationName.value.get(row.station_code) || row.station_code,
  }))
  return list.sort((a, b) => (b.observed_availability ?? -1) - (a.observed_availability ?? -1))
})

/* ---- 设备可用率表：报表要看「哪台设备在拖」，场站汇总看不出个体 ----
   接口一次返回窗口内全部设备行，搜索/排序/分页都在本地做；窗口与筛选已经在后端生效。 */
const deviceKeyword = ref('')
const deviceSort = ref<'asc' | 'desc'>('asc')
const devicePage = ref(1)
const devicePageSize = 12

/** 台账下拉里带设备名，这里做 id → 名称映射；没拉到名字就退回编码 */
const deviceName = computed(() => {
  const map = new Map<number, string>()
  for (const item of deviceOptions.value) map.set(item.id, item.name)
  return map
})

const deviceRows = computed<AvailabilityItem[]>(() => {
  const key = deviceKeyword.value.trim().toUpperCase()
  const list = (availability.value?.devices ?? []).filter((row) => {
    if (!key) return true
    return (
      row.device_code.toUpperCase().includes(key) ||
      row.station_code.includes(key) ||
      (deviceName.value.get(row.device_id) || '').toUpperCase().includes(key)
    )
  })
  /* 缺值的设备排最后：可用率算不出来不等于「0% 可用」，不能排进最差那一头 */
  return [...list].sort((a, b) => {
    const left = a.observed_availability
    const right = b.observed_availability
    if (left == null && right == null) return a.device_code.localeCompare(b.device_code)
    if (left == null) return 1
    if (right == null) return -1
    return deviceSort.value === 'asc' ? left - right : right - left
  })
})

const devicePages = computed(() => Math.max(1, Math.ceil(deviceRows.value.length / devicePageSize)))
const devicePaged = computed(() =>
  deviceRows.value.slice((devicePage.value - 1) * devicePageSize, devicePage.value * devicePageSize),
)
watch(devicePages, (value) => {
  if (devicePage.value > value) devicePage.value = value
})
/* 换窗口/换场站等于换了一批设备，页码要回到第一页，否则会停在空页 */
watch([() => availability.value, () => deviceKeyword.value, () => deviceSort.value], () => {
  devicePage.value = 1
})

const totals = computed(() => {
  const list = availability.value?.stations ?? []
  const covered = list.reduce((sum, row) => sum + row.covered_ms, 0)
  const span = list.reduce((sum, row) => sum + row.window_ms, 0)
  const running = list.reduce((sum, row) => sum + row.running_ms, 0)
  const stopped = list.reduce((sum, row) => sum + row.stopped_ms, 0)
  const fault = list.reduce((sum, row) => sum + row.fault_ms, 0)
  const unknown = list.reduce((sum, row) => sum + row.unknown_ms, 0)
  return {
    /* 与 report.go 同口径：可用率 =（运行 + 停机）÷ 覆盖时长。
       停机算「设备可用」但不算发电；用「运行 ÷ 覆盖」会和下面每行场站自己的比率对不上。 */
    availability: covered ? (running + stopped) / covered : null,
    coverage: span ? covered / span : null,
    fault,
    unknown,
    devices: list.reduce((sum, row) => sum + row.device_count, 0),
  }
})

const availabilityStrip = computed(() => {
  const list = availability.value?.stations ?? []
  const covered = list.reduce((sum, row) => sum + row.covered_ms, 0)
  const span = list.reduce((sum, row) => sum + row.window_ms, 0)
  const fault = list.reduce((sum, row) => sum + row.fault_ms, 0)
  const unknown = list.reduce((sum, row) => sum + row.unknown_ms, 0)
  return [
    { key: 'avail', label: '整体可用率', color: 'var(--ok)', value: totals.value.availability == null ? '—' : pct(totals.value.availability * 100, 2), note: '非故障 / 有数据（按设备时长汇总）' },
    { key: 'cover', label: '数据覆盖', color: 'var(--brand)', value: totals.value.coverage == null ? '—' : pct(totals.value.coverage * 100, 1), note: '有数据时长 / 窗口时长' },
    { key: 'fault', label: '故障占比', color: 'var(--bad)', value: covered ? pct((fault / covered) * 100, 2) : '—', note: '故障时长 / 有数据时长' },
    { key: 'unknown', label: '未知占比', color: 'var(--ink-4)', value: span ? pct((unknown / span) * 100, 2) : '—', note: '无数据，不计入可用率分母' },
  ]
})

const timeRows = computed(() => {
  const result = timeliness.value?.result
  if (!result) return []
  return [
    { key: 'ack', label: '告警确认耗时', note: '触发 → 确认', stat: result.ack_timeliness },
    { key: 'recover', label: '平台告警恢复耗时（合计）', note: '触发 → 恢复', stat: result.recover_timeliness },
    { key: 'telemetry', label: '遥测恢复耗时', note: '遥测回到正常区间', stat: result.telemetry_recover_timeliness },
    { key: 'prediction', label: '预测风险下降耗时', note: '预测概率回落到阈值下', stat: result.prediction_recover_timeliness },
  ]
})

const timelinessStrip = computed(() => {
  const result = timeliness.value?.result
  return [
    { key: 'ack', label: '确认中位', color: 'var(--brand)', value: result ? dur(result.ack_timeliness.median_ms) : '—', note: result ? `${result.ack_timeliness.count} 条已确认` : '' },
    { key: 'recover', label: '恢复中位', color: 'var(--ok)', value: result ? dur(result.recover_timeliness.median_ms) : '—', note: result ? `${result.recover_timeliness.count} 条已恢复` : '' },
    { key: 'pendingAck', label: '待确认', color: 'var(--warn)', value: result ? `${result.pending_ack}` : '—', note: '超过窗口仍未确认' },
    { key: 'pendingRecover', label: '待恢复', color: 'var(--bad)', value: result ? `${result.pending_recover}` : '—', note: '已确认未恢复' },
  ]
})

const recoveryReasons = computed(() => {
  const map = timeliness.value?.result.recovery_reasons || {}
  const label: Record<string, string> = {
    telemetry_normal: '遥测回到正常',
    prediction_low: '预测概率回落',
    manual: '人工关闭',
  }
  const total = Object.values(map).reduce((sum, value) => sum + value, 0) || 1
  return Object.entries(map).map(([key, count]) => ({
    key,
    label: label[key] || key,
    count,
    width: (count / total) * 100,
  }))
})

/** 每个页签各自一个导出按钮：导出的是该页签的当前数据，不跟当前激活页签耦合 */
const exporting = ref<TabKey | ''>('')

/** 后端不可用时的兜底：只导当前页已有的行，字段比后端 CSV 少 */
const localCsv = (kind: TabKey) => {
  let header = ''
  let lines: string[] = []
  if (kind === 'availability') {
    header = 'station_code,station_name,device_count,availability,data_coverage,fault_ms,unknown_ms'
    lines = stationRows.value.map((row) =>
      [
        row.station_code,
        row.name,
        row.device_count,
        row.observed_availability,
        row.data_coverage,
        row.fault_ms,
        row.unknown_ms,
      ].join(','),
    )
  } else {
    header = 'metric,count,mean_ms,median_ms,p90_ms'
    lines = timeRows.value.map((row) =>
      [row.key, row.stat.count, row.stat.mean_ms, row.stat.median_ms, row.stat.p90_ms].join(','),
    )
  }
  const blob = new Blob([`\ufeff${[header, ...lines].join('\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `report-${kind}-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

/** 优先走后端 CSV（含设备行 + 场站汇总行 + 规则版本），失败才退回本地拼 */
const exportCsv = async (kind: TabKey = tab.value) => {
  if (exporting.value) return
  const range = rangeParams()
  if (!range) return
  exporting.value = kind
  try {
    await api.exportReport(kind === 'availability' ? 'availability' : 'alarm-timeliness', {
      ...range,
      ...deviceQuery(),
    })
  } catch (error) {
    localCsv(kind)
    toast('warn', `后端未返回 CSV，已按当前页导出：${(error as Error).message}`)
  } finally {
    exporting.value = ''
  }
}
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" :disabled="loading" @click="load">
        <Icon name="refresh" :size="15" />刷新
      </button>
    </div>
  </div>

  <div v-if="failed" class="error-box">报表未取到：{{ failed }}</div>

  <section class="card filter-bar">
    <div class="filter-row">
      <label class="filter-field">
        <span class="filter-label">开始</span>
        <input v-model="startText" class="field filter-input" type="datetime-local" />
      </label>
      <label class="filter-field">
        <span class="filter-label">结束</span>
        <input v-model="endText" class="field filter-input" type="datetime-local" />
      </label>
      <select v-model="stationFilter" class="field filter-select" @change="onStationChange">
        <option value="">全部场站</option>
        <option v-for="item in stations" :key="item.station_code" :value="item.station_code">{{ item.station_name }}</option>
      </select>
      <select v-model="deviceFilter" class="field filter-select filter-device">
        <option value="">全部设备</option>
        <option v-for="item in deviceOptions" :key="item.id" :value="String(item.id)">{{ item.code }} · {{ item.name }}</option>
      </select>
      <button class="btn btn-primary" :disabled="loading" @click="load">查询</button>
    </div>
    <p v-if="rangeError" class="filter-error">{{ rangeError }}</p>
  </section>

  <div class="report-tabs">
    <div class="seg">
      <button v-for="item in tabs" :key="item.key" :class="{ 'is-on': tab === item.key }" @click="tab = item.key">
        {{ item.label }}
      </button>
    </div>
    <span class="period">统计窗口 {{ windowLabel }} · 规则版本 {{ availability?.rule_version || timeliness?.rule_version || '—' }}</span>
  </div>

  <template v-if="tab === 'availability'">
    <section class="card strip">
      <div v-for="item in availabilityStrip" :key="item.key" class="strip-cell">
        <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
        <span class="strip-value num">{{ item.value }}</span>
        <span class="strip-note">{{ item.note }}</span>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">场站可用率</span>
        <span class="card-sub">{{ stationRows.length }} 个场站 · {{ totals.devices }} 台设备</span>
        <div class="card-tools">
          <button class="btn btn-sm" :disabled="!stationRows.length || !!exporting" @click="exportCsv('availability')">
            <Icon name="download" :size="13" />导出可用率 CSV
          </button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table rp-table">
          <colgroup>
            <col style="width: 26%" />
            <col style="width: 9%" />
            <col style="width: 13%" />
            <col style="width: 12%" />
            <col style="width: 12%" />
            <col style="width: 12%" />
            <col style="width: 16%" />
          </colgroup>
          <thead>
            <tr>
              <th>场站</th>
              <th class="r">设备</th>
              <th class="r">运行台时</th>
              <th class="r">停机台时</th>
              <th class="r">故障台时</th>
              <th class="r">未知台时</th>
              <th>可用率</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in stationRows" :key="row.station_code">
              <td>
                <div class="cell-stack">
                  <span class="cell-name">{{ row.name }}</span>
                  <span class="cell-note num">{{ row.station_code }} · 覆盖 {{ pct(coverageOf(row) * 100, 1) }}</span>
                </div>
              </td>
              <td class="r num">{{ row.device_count }}</td>
              <td class="r num">{{ dh(row.running_ms) }}</td>
              <td class="r num">{{ dh(row.stopped_ms) }}</td>
              <td class="r num fault">{{ dh(row.fault_ms) }}</td>
              <td class="r num muted">{{ dh(row.unknown_ms) }}</td>
              <td>
                <div class="avail-cell">
                  <span class="meter" :class="availOf(row) >= 0.98 ? 't-ok' : availOf(row) >= 0.9 ? '' : 't-bad'">
                    <i :style="{ width: `${availOf(row) * 100}%` }"></i>
                  </span>
                  <b class="num">{{ pct(availOf(row) * 100, 2) }}</b>
                </div>
              </td>
            </tr>
            <tr v-if="!stationRows.length && !loading">
              <td colspan="7" class="empty">暂无可用率数据</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>计算于 {{ stamp(availability?.computed_at) }}</span>
        <span class="grow"></span>
        <span class="pager-total">台·时 = 设备数 × 小时，跨设备累加；未知不计入分母，避免把「没数据」算成「停机」</span>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <span class="card-title">设备可用率</span>
        <span class="card-sub">
          {{ deviceRows.length }} 台匹配 · 共 {{ availability?.devices.length ?? 0 }} 台
        </span>
        <div class="card-tools">
          <button
            class="btn btn-sm"
            :disabled="!deviceRows.length"
            :title="deviceSort === 'asc' ? '当前按可用率从低到高，点击改为从高到低' : '当前按可用率从高到低，点击改为从低到高'"
            @click="deviceSort = deviceSort === 'asc' ? 'desc' : 'asc'"
          >
            <Icon name="chevronDown" :size="13" :style="{ transform: deviceSort === 'asc' ? 'rotate(180deg)' : 'none' }" />
            {{ deviceSort === 'asc' ? '升序' : '降序' }}
          </button>
          <label class="field-search">
            <Icon name="search" :size="14" />
            <input v-model="deviceKeyword" type="text" placeholder="编码 / 名称 / 场站" />
          </label>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table dv-table">
          <colgroup>
            <col style="width: 28%" />
            <col style="width: 19%" />
            <col style="width: 11%" />
            <col style="width: 11%" />
            <col style="width: 11%" />
            <col style="width: 11%" />
            <col style="width: 9%" />
          </colgroup>
          <thead>
            <tr>
              <th>设备</th>
              <th>可用率</th>
              <th class="r">运行</th>
              <th class="r">停机</th>
              <th class="r">故障</th>
              <th class="r">未知</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in devicePaged" :key="row.device_id">
              <td>
                <div class="cell-stack">
                  <span class="cell-name">{{ deviceName.get(row.device_id) || row.device_code }}</span>
                  <span class="cell-note num">
                    {{ row.device_code }} · 覆盖 {{ pct(coverageOf(row) * 100, 1) }}
                  </span>
                </div>
              </td>
              <td>
                <div class="avail-cell">
                  <span class="meter" :class="availOf(row) >= 0.98 ? 't-ok' : availOf(row) >= 0.9 ? '' : 't-bad'">
                    <i :style="{ width: `${availOf(row) * 100}%` }"></i>
                  </span>
                  <b class="num">{{ row.observed_availability == null ? '—' : pct(availOf(row) * 100, 2) }}</b>
                </div>
              </td>
              <td class="r num">{{ dur(row.running_ms) }}</td>
              <td class="r num">{{ dur(row.stopped_ms) }}</td>
              <td class="r num fault">{{ dur(row.fault_ms) }}</td>
              <td class="r num muted">{{ dur(row.unknown_ms) }}</td>
              <td>
                <StatusChip v-if="row.insufficient_data" tone="idle" :dot="false" label="数据不足" />
                <span v-else class="muted">有效</span>
              </td>
            </tr>
            <tr v-if="!devicePaged.length && !loading">
              <td colspan="7" class="empty">没有符合条件的设备</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button class="pager-btn" :disabled="devicePage <= 1" title="上一页" @click="devicePage -= 1">
          <Icon name="chevronLeft" :size="15" />
        </button>
        <span class="pager-page num">{{ devicePage }}</span>
        <span class="pager-total">/ {{ devicePages }}</span>
        <button class="pager-btn" :disabled="devicePage >= devicePages" title="下一页" @click="devicePage += 1">
          <Icon name="chevronRight" :size="15" />
        </button>
        <span class="grow"></span>
        <span class="pager-total">每页 {{ devicePageSize }} 台 · 覆盖率低于 90% 记为数据不足</span>
      </div>
    </section>
  </template>

  <template v-else>
    <section class="card strip">
      <div v-for="item in timelinessStrip" :key="item.key" class="strip-cell">
        <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
        <span class="strip-value num">{{ item.value }}</span>
        <span class="strip-note">{{ item.note }}</span>
      </div>
    </section>

    <div class="tm-grid">
      <section class="card">
        <div class="card-head">
          <span class="card-title">时效分布</span>
          <span class="card-sub">单位：时:分:秒</span>
          <div class="card-tools">
            <button class="btn btn-sm" :disabled="!timeRows.length || !!exporting" @click="exportCsv('timeliness')">
              <Icon name="download" :size="13" />导出时效 CSV
            </button>
          </div>
        </div>
        <div class="table-wrap">
          <table class="table tm-table">
            <colgroup>
              <col style="width: 40%" />
              <col style="width: 15%" />
              <col style="width: 15%" />
              <col style="width: 15%" />
              <col style="width: 15%" />
            </colgroup>
            <thead>
              <tr>
                <th>指标</th>
                <th class="c">有效样本数</th>
                <th class="c">平均值</th>
                <th class="c">中位数</th>
                <th class="c">P90</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in timeRows" :key="row.key">
                <td>
                  <div class="cell-stack">
                    <span class="cell-name">{{ row.label }}</span>
                    <span class="cell-note">{{ row.note }}</span>
                  </div>
                </td>
                <td class="c num">{{ row.stat.count }}</td>
                <td class="c num">{{ dur(row.stat.mean_ms) }}</td>
                <td class="c num strong">{{ dur(row.stat.median_ms) }}</td>
                <td class="c num">{{ dur(row.stat.p90_ms) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="pager">
          <span>事件 {{ timeliness?.result.event_count ?? '—' }} 条 · 无效生命周期 {{ timeliness?.result.invalid_lifecycle ?? '—' }} 条</span>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">恢复原因</span>
          <span class="card-sub">按关闭原因拆分</span>
        </div>
        <div class="reasons">
          <div v-for="item in recoveryReasons" :key="item.key" class="reason">
            <div class="reason-head">
              <span>{{ item.label }}</span>
              <b class="num">{{ item.count }}</b>
            </div>
            <span class="reason-track"><i :style="{ width: `${item.width}%` }"></i></span>
          </div>
          <div class="reason-foot">
            <span>人工管理/未知恢复</span>
            <b class="num">
              管理 {{ timeliness?.result.managed_recover ?? 0 }} · 未知 {{ timeliness?.result.unknown_recover ?? 0 }}
            </b>
          </div>
        </div>
      </section>
    </div>
  </template>

  <details class="page-note">
    <summary>口径说明</summary>
    <p>
      可用率 = 运行时长 ÷ 有数据时长；有数据时长 = 窗口时长 − 未知时长。故障时长计入不可用但不含人工停机。
      设备表按设备自己的有效窗口算，场站表把下属设备的时长求和后再相除（不是逐台比率取平均）。
      覆盖率低于 90% 的设备标「数据不足」，比率照实计算但不建议与满覆盖设备直接比较。
      及时性只统计完成完整生命周期的告警，未确认/未恢复的单列，避免用未结束的事件拉高均值。
    </p>
  </details>
</template>

<style scoped>
/* 筛选栏：日期 + 场站 + 设备 + 查询，条件即接口参数（start/end/station_code/device_id） */
.filter-bar {
  padding: 14px 16px;
  margin-bottom: 14px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.filter-field {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-label {
  font-size: 11.5px;
  color: var(--ink-3);
  white-space: nowrap;
}

.filter-input {
  min-width: 180px;
}

.filter-select {
  min-width: 150px;
}

.filter-device {
  min-width: 226px;
  max-width: 300px;
}

.filter-error {
  margin: 9px 0 0;
  font-size: 11.5px;
  color: var(--bad);
}

.report-tabs {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.period {
  font-size: 11.5px;
  color: var(--ink-3);
}

.strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.strip-cell {
  padding: 15px 20px 14px;
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

.rp-table,
.tm-table,
.dv-table {
  table-layout: fixed;
  min-width: 760px;
}

/* 设备表把「可用率」放在设备后面：窄屏下先看到比分，再横向滚动看时长明细 */
.dv-table {
  min-width: 840px;
}

.rp-table .r,
.tm-table .r,
.dv-table .r {
  text-align: right;
}

/* 时长与计数居中：位数固定后竖排能对齐，右对齐反而让「—」和 00:00:00 错开 */
.tm-table .c {
  text-align: center;
}

.fault {
  color: var(--bad);
}

.muted {
  color: var(--ink-4);
}

.strong {
  font-weight: 700;
  color: var(--ink);
}

.avail-cell {
  display: flex;
  align-items: center;
  gap: 9px;
}

.avail-cell .meter {
  flex: 1;
  min-width: 0;
  max-width: 96px;
}

.avail-cell b {
  font-size: 12.5px;
  color: var(--ink);
  min-width: 52px;
  text-align: right;
}

.tm-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.reasons {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 20px 18px;
}

.reason {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.reason-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 12px;
  color: var(--ink-2);
}

.reason-head b {
  color: var(--ink);
}

.reason-track {
  height: 7px;
  border-radius: 4px;
  background: var(--line-soft);
  overflow: hidden;
}

.reason-track i {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--brand);
}

.reason-foot {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-top: 4px;
  padding-top: 10px;
  border-top: 1px solid var(--line-soft);
  font-size: 11.5px;
  color: var(--ink-3);
}

.reason-foot b {
  color: var(--ink-2);
  font-weight: 600;
}

@media (max-width: 1240px) {
  .tm-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 760px) {
  .strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .strip-cell:nth-child(3) {
    border-left: 0;
    border-top: 1px solid var(--line-soft);
  }

  .strip-cell:nth-child(4) {
    border-top: 1px solid var(--line-soft);
  }
}
</style>
