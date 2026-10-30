<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import Pager from '../components/Pager.vue'
import StatusChip from '../components/StatusChip.vue'
import { api } from '../lib/api'
import type { Device, HealthRow } from '../lib/api'
import { deviceIndex } from '../lib/data'
import { HEALTH_STATE_CN, n, pct, toneOf } from '../lib/format'

const route = useRoute()
const router = useRouter()

const devices = ref<Device[]>([])
const healthIndex = ref<Map<number, HealthRow>>(new Map())
const loading = ref(true)
const failed = ref('')

const station = ref('all')
/** 状态直接取真后端 health.go 的 state：online / offline / fault / unknown */
const stateOptions = ['all', 'online', 'offline', 'fault', 'unknown'] as const
const STATE_LABEL: Record<string, string> = {
  all: '全部状态', online: '在线', offline: '离线', fault: '故障', unknown: '未知',
}
const stateFilter = ref<(typeof stateOptions)[number]>('all')
const keyword = ref('')
const sortBy = ref<'health-asc' | 'health-desc' | 'code'>('health-asc')
const page = ref(1)
const pageSize = 8

/* ---------------- 载入 ---------------- */

const load = async () => {
  loading.value = true
  try {
    const [index, healthPayload] = await Promise.all([deviceIndex(), api.healthDevices()])
    devices.value = [...index.values()].sort((a, b) => a.id - b.id)
    healthIndex.value = new Map(healthPayload.devices.map((row) => [row.device_id, row]))
    failed.value = ''
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}

const stationOptions = computed(() => {
  const codes = Array.from(new Set(devices.value.map((item) => item.station_code)))
  return ['all', ...codes.sort()]
})

const stateOf = (row: HealthRow | null) => (row ? HEALTH_STATE_CN[row.state] || row.state : '未知')

const toneOfStatus = (row: HealthRow | null) => toneOf(row?.state || 'unknown')

/** 进度条只用三档：正常 / 中性 / 异常，避免整张表都是警示橙。 */
const meterTone = (score: number | null | undefined) => {
  if (score == null) return ''
  if (score >= 88) return 't-ok'
  if (score >= 72) return ''
  return 't-bad'
}

const rows = computed(() => {
  const key = keyword.value.trim().toLowerCase()
  return devices.value
    .map((device) => ({ device, health: healthIndex.value.get(device.id) || null }))
    .filter((row) => station.value === 'all' || row.device.station_code === station.value)
    .filter((row) => stateFilter.value === 'all' || (row.health?.state || 'unknown') === stateFilter.value)
    .filter((row) => {
      if (!key) return true
      return [row.device.device_code, row.device.name, row.device.vendor]
        .some((field) => field.toLowerCase().includes(key))
    })
    .sort((a, b) => {
      if (sortBy.value === 'code') return a.device.device_code.localeCompare(b.device.device_code)
      const sa = a.health?.score ?? 999
      const sb = b.health?.score ?? 999
      return sortBy.value === 'health-asc' ? sa - sb : sb - sa
    })
})

const totalPages = computed(() => Math.max(1, Math.ceil(rows.value.length / pageSize)))
const paged = computed(() => rows.value.slice((page.value - 1) * pageSize, page.value * pageSize))

watch([station, stateFilter, keyword, sortBy], () => {
  page.value = 1
})

/* ---------------- 概览条 ---------------- */

const strip = computed(() => {
  const scores = Array.from(healthIndex.value.values())
    .map((row) => row.score)
    .filter((score): score is number => score != null)
  const avg = scores.length ? scores.reduce((sum, value) => sum + value, 0) / scores.length : 0
  const rows = Array.from(healthIndex.value.values())
  const online = rows.filter((row) => row.state === 'online').length
  const fault = rows.filter((row) => row.state === 'fault').length
  const total = devices.value.length
  return [
    { key: 'total', label: '在册设备', value: n(total), note: `${stationOptions.value.length - 1} 个场站`, color: 'var(--brand)' },
    {
      key: 'online',
      label: '在线',
      value: n(online),
      note: total ? `占在册 ${pct((online / total) * 100, 1)}` : '—',
      color: 'var(--ok)',
    },
    { key: 'offline', label: '离线 / 故障', value: n(total - online), note: fault ? `其中故障 ${fault} 台` : '需现场核查', color: 'var(--warn)' },
    { key: 'health', label: '平均健康分', value: avg ? avg.toFixed(1) : '—', note: `${scores.length} 台有效`, color: 'var(--brand)' },
  ]
})

/* ---------------- 跳转 ---------------- */

const open = (device: Device) => {
  router.push({ path: `/devices/${device.id}` })
}

const applyQuery = async () => {
  if (typeof route.query.station === 'string') station.value = route.query.station
  if (typeof route.query.q === 'string') keyword.value = route.query.q
  /* 顶栏「离线设备」等入口带 ?state= 进来，非法值忽略 */
  const state = typeof route.query.state === 'string' ? route.query.state : ''
  if (stateOptions.includes(state as (typeof stateOptions)[number])) {
    stateFilter.value = state as (typeof stateOptions)[number]
  }
  await load()
}

onMounted(applyQuery)

watch(() => route.query.q, (value) => {
  keyword.value = typeof value === 'string' ? value : ''
  page.value = 1
})

function scoreWidth(score: number | null | undefined) {
  return `${score == null ? 0 : Math.max(3, score)}%`
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

  <div v-if="failed" class="error-box">设备数据未取到：{{ failed }}</div>

  <section class="card strip">
    <div v-for="item in strip" :key="item.key" class="strip-cell">
      <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
      <span class="strip-value num">{{ item.value }}</span>
      <span class="strip-note">{{ item.note }}</span>
    </div>
  </section>

  <section class="card">
    <div class="card-head">
      <span class="card-title">设备清单</span>
      <span class="card-sub">{{ rows.length }} 台匹配</span>
      <div class="card-tools">
        <label class="field-search">
          <Icon name="search" :size="14" />
          <input v-model="keyword" type="text" placeholder="编码 / 名称 / 厂商" />
        </label>
        <select v-model="sortBy" class="select" aria-label="排序方式">
          <option value="health-asc">健康分从低到高</option>
          <option value="health-desc">健康分从高到低</option>
          <option value="code">按设备编码</option>
        </select>
      </div>
    </div>

    <div class="filters">
      <div class="seg scroll-x">
        <button
          v-for="code in stationOptions"
          :key="code"
          :class="{ 'is-on': station === code }"
          @click="station = code"
        >
          {{ code === 'all' ? '全部场站' : code }}
        </button>
      </div>
      <div class="seg">
        <button
          v-for="item in stateOptions"
          :key="item"
          :class="{ 'is-on': stateFilter === item }"
          @click="stateFilter = item"
        >
          {{ STATE_LABEL[item] }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="loading">读取设备台账…</div>
    <div v-else class="table-wrap">
      <table class="table device-table">
        <colgroup>
          <col style="width: 32%" />
          <col style="width: 9%" />
          <col style="width: 11%" />
          <col style="width: 18%" />
          <col style="width: 12%" />
          <col style="width: 14%" />
          <col style="width: 4%" />
        </colgroup>
        <thead>
          <tr>
            <th>设备</th>
            <th>场站</th>
            <th>组</th>
            <th>健康分</th>
            <th>状态</th>
            <th>厂商</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in paged"
            :key="row.device.id"
            class="is-click"
            :title="`查看 ${row.device.name} 详情`"
            @click="open(row.device)"
          >
            <td>
              <div class="cell-main">
                <!-- 图标只表达设备类型，健康分级交给进度条与状态胶囊 -->
                <span class="cell-icon">
                  <Icon name="sun" :size="15" />
                </span>
                <span class="cell-stack">
                  <span class="cell-name">{{ row.device.name }}</span>
                  <span class="cell-note">{{ row.device.device_code }}</span>
                </span>
              </div>
            </td>
            <td class="muted tight">{{ row.device.station_code }}</td>
            <td class="muted tight">{{ row.device.group_name }}</td>
            <td>
              <div class="meter-row">
                <span class="meter" :class="meterTone(row.health?.score)">
                  <i :style="{ width: scoreWidth(row.health?.score) }"></i>
                </span>
                <span class="num">{{ row.health?.score == null ? '—' : n(row.health.score, 0) }}</span>
              </div>
            </td>
            <td>
              <StatusChip :tone="toneOfStatus(row.health)" :label="stateOf(row.health)" />
            </td>
            <td class="muted tight">{{ row.device.vendor }}</td>
            <td class="arrow"><Icon name="chevronRight" :size="14" /></td>
          </tr>
          <tr v-if="!paged.length">
            <td colspan="7" class="empty">当前筛选下没有设备。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pager
      v-model:page="page"
      :total-pages="totalPages"
      :total="rows.length"
      :page-size="pageSize"
      unit="台"
    />
  </section>
</template>

<style scoped>
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

.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 0 18px 14px;
}

.scroll-x {
  max-width: 100%;
  overflow-x: auto;
}

.device-table {
  table-layout: fixed;
}

.device-table td {
  padding-top: 9px;
  padding-bottom: 9px;
  overflow: hidden;
}

.device-table .arrow {
  color: var(--ink-4);
  text-align: right;
}

.device-table tbody tr:hover .arrow {
  color: var(--brand);
}

@media (max-width: 1100px) {
  .device-table {
    min-width: 880px;
  }
}

@media (max-width: 900px) {
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

@media (max-width: 680px) {
  .device-table {
    min-width: 720px;
  }

  .card-tools {
    flex-wrap: wrap;
  }

  /* 场站胶囊横向可滑：右缘渐隐提示还有内容 */
  .scroll-x {
    -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 20px), transparent);
    mask-image: linear-gradient(to right, #000 calc(100% - 20px), transparent);
  }
}
</style>
