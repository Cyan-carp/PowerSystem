<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import Drawer from '../components/Drawer.vue'
import Pager from '../components/Pager.vue'
import StatusChip from '../components/StatusChip.vue'
import DonutGauge from '../components/DonutGauge.vue'
import { api } from '../lib/api'
import type { HealthBoard, HealthRow, StationRow } from '../lib/api'
import { HEALTH_STATE_CN, LEVEL_CN, TELEMETRY_STATE_CN, clockMs, healthBasis, healthBasisShort, n, stamp, toneOf } from '../lib/format'

const route = useRoute()
const router = useRouter()

const board = ref<HealthBoard | null>(null)
const stations = ref<StationRow[]>([])
const loading = ref(true)
const failed = ref('')

/* 状态四分沿用旧前端的口径：有评分 / 数据不足 / 连接离线 / 遥测故障。
   「有评分」包含离线与故障（它们记 0 分），所以四档互斥但不等于 online 一种状态——
   用 online / offline / fault / unknown 四个原始 state 是分不出「离线但有评分」和「离线且没数据」的。 */
type StateKey = 'all' | 'scored' | 'unknown' | 'offline' | 'fault'
const stateOptions: StateKey[] = ['all', 'scored', 'unknown', 'offline', 'fault']
const STATE_LABEL: Record<StateKey, string> = {
  all: '全部', scored: '有评分', unknown: '数据不足', offline: '离线', fault: '故障',
}
const stateFilter = ref<StateKey>('all')
const keyword = ref('')
const page = ref(1)
const pageSize = 8
const selected = ref<HealthRow | null>(null)

const load = async () => {
  loading.value = true
  try {
    const [health, stationPayload] = await Promise.all([api.healthDevices(), api.stations()])
    board.value = health
    stations.value = stationPayload.list
    failed.value = ''
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/* 筛选状态写进地址栏，刷新与分享都能还原 */
const applyQuery = () => {
  const state = route.query.state as string | undefined
  if (state && stateOptions.includes(state as StateKey)) stateFilter.value = state as StateKey
  const q = route.query.q as string | undefined
  if (q) keyword.value = q
}
applyQuery()
watch(
  () => route.query,
  () => {
    applyQuery()
    page.value = 1
  },
)
watch([stateFilter, keyword], () => {
  page.value = 1
  const query: Record<string, string> = {}
  if (stateFilter.value !== 'all') query.state = stateFilter.value
  if (keyword.value.trim()) query.q = keyword.value.trim()
  router.replace({ query })
})

const stationName = computed(() => {
  const map = new Map<string, string>()
  for (const item of stations.value) map.set(item.station_code, item.station_name)
  return map
})

const allRows = computed<HealthRow[]>(() => board.value?.devices ?? [])

/** 没分可算就是「数据不足」：state=unknown 与 score 为空在界面上是同一件事 */
const isUnknownRow = (row: HealthRow) => row.state === 'unknown' || row.score == null

const matchesState = (row: HealthRow): boolean => {
  if (stateFilter.value === 'all') return true
  if (stateFilter.value === 'scored') return !isUnknownRow(row)
  if (stateFilter.value === 'unknown') return isUnknownRow(row)
  return row.state === stateFilter.value
}

const rows = computed(() => {
  const key = keyword.value.trim().toUpperCase()
  return allRows.value.filter((row) => {
    if (!matchesState(row)) return false
    if (!key) return true
    return (
      row.device_code.toUpperCase().includes(key) ||
      (row.name || '').toUpperCase().includes(key) ||
      row.station_code.includes(key)
    )
  })
})

const paged = computed(() => rows.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const totalPages = computed(() => Math.max(1, Math.ceil(rows.value.length / pageSize)))
watch(totalPages, (value) => {
  if (page.value > value) page.value = value
})

const scoredRows = computed(() => allRows.value.filter((row) => row.score != null))
const average = computed(() => {
  const scored = scoredRows.value.map((row) => row.score as number)
  if (!scored.length) return null
  return scored.reduce((sum, score) => sum + score, 0) / scored.length
})

const countOf = (state: string) => allRows.value.filter((row) => row.state === state).length

/* 规则分只有 100 / 90 / 70 / 40 / 0 五档（rule-health-v1），不是连续分布；
   档位只按分数归类，与 state 无关，用来一眼看出「掉分集中在哪一档」。 */
const bands = computed(() => {
  const defs = [
    { score: 100, label: '在线无告警', color: 'var(--ok)' },
    { score: 90, label: '一般告警', color: 'var(--brand)' },
    { score: 70, label: '重要告警', color: 'var(--warn)' },
    { score: 40, label: '紧急告警', color: 'var(--warn-deep)' },
    { score: 0, label: '离线或故障', color: 'var(--bad)' },
  ]
  const total = scoredRows.value.length || 1
  return defs.map((item) => {
    const count = scoredRows.value.filter((row) => row.score === item.score).length
    return { ...item, count, share: (count / total) * 100 }
  })
})

const strip = computed(() => [
  {
    key: 'avg', label: '平均健康分', color: 'var(--brand)',
    value: average.value == null ? '—' : n(average.value, 1),
    note: `${scoredRows.value.length} 台有评分 · 未知 ${allRows.value.length - scoredRows.value.length} 台`,
  },
  { key: 'online', label: '在线', color: 'var(--ok)', value: `${countOf('online')}`, note: '按最高活动告警评级' },
  { key: 'offline', label: '离线', color: 'var(--ink-placeholder)', value: `${countOf('offline')}`, note: '接收超 15 秒，计 0 分' },
  { key: 'fault', label: '故障', color: 'var(--bad)', value: `${countOf('fault')}`, note: '最新遥测 status=2' },
])

/* 与后端 aggregateHealth 的排序一致：正式分在前、暂定分在后，再按分数降序 */
const stationRows = computed(() => {
  const list = (board.value?.stations ?? []).map((row) => ({
    code: row.station_code,
    name: stationName.value.get(row.station_code) || row.station_code,
    score: row.score,
    valid: row.valid_count,
    total: row.device_total,
    provisional: row.provisional,
  }))
  return list.sort((a, b) => {
    if (a.provisional !== b.provisional) return a.provisional ? 1 : -1
    return (b.score ?? -1) - (a.score ?? -1)
  })
})

const scoreTone = (score: number | null) => {
  if (score == null) return 'idle'
  if (score >= 88) return 'ok'
  if (score >= 72) return 'brand'
  return 'bad'
}

const chipTone = (state: string) => toneOf(state)

const exportCsv = () => {
  if (!rows.value.length) return
  const header = 'rank,device_code,name,station_code,score,state,matched_by,active_level,sampled_at_ms'
  const lines = rows.value.map((row) =>
    [
      row.rank ?? '',
      row.device_code,
      row.name || '',
      row.station_code,
      row.score ?? '',
      row.state,
      row.matched_by,
      row.active_level ?? '',
      row.sampled_at_ms ?? '',
    ].join(','),
  )
  const blob = new Blob([`\ufeff${[header, ...lines].join('\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `health-ranking-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

const openRow = (row: HealthRow) => {
  selected.value = row
  router.replace({ query: { ...route.query, id: String(row.device_id) } })
}
const closeDrawer = () => {
  selected.value = null
  const query = { ...route.query }
  delete query.id
  router.replace({ query })
}
watch(
  () => [board.value, route.query.id] as const,
  () => {
    const id = Number(route.query.id)
    if (!id) return
    const hit = allRows.value.find((row) => row.device_id === id)
    if (hit) selected.value = hit
  },
)
</script>

<template>
  <div class="page-head">
    <div class="page-actions">
      <button class="btn" @click="exportCsv" :disabled="!rows.length">
        <Icon name="download" :size="15" />导出
      </button>
      <button class="btn" :disabled="loading" @click="load">
        <Icon name="refresh" :size="15" />刷新
      </button>
    </div>
  </div>

  <div v-if="failed" class="error-box">健康数据未取到：{{ failed }}</div>

  <section class="card strip">
    <div v-for="item in strip" :key="item.key" class="strip-cell">
      <span class="strip-label"><i :style="{ background: item.color }"></i>{{ item.label }}</span>
      <span class="strip-value num">{{ item.value }}</span>
      <span class="strip-note">{{ item.note }}</span>
    </div>
  </section>

  <div class="hl-grid">
    <section class="card">
      <div class="card-head">
        <span class="card-title">设备排名</span>
        <span class="card-sub">{{ rows.length }} 台匹配 · 共 {{ allRows.length }} 台</span>
        <div class="card-tools">
          <label class="field-search">
            <Icon name="search" :size="14" />
            <input v-model="keyword" type="text" placeholder="编码 / 名称 / 场站" />
          </label>
        </div>
      </div>

      <div class="filters">
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

      <div class="table-wrap">
        <table class="table hl-table">
          <colgroup>
            <col style="width: 8%" />
            <col style="width: 30%" />
            <col style="width: 16%" />
            <col style="width: 12%" />
            <col style="width: 22%" />
            <col style="width: 12%" />
          </colgroup>
          <thead>
            <tr>
              <th>名次</th>
              <th>设备</th>
              <th>健康分</th>
              <th>状态</th>
              <th>评分依据</th>
              <th class="c">采样时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in paged" :key="row.device_id" class="row-link" @click="openRow(row)">
              <td class="num rank" :class="{ 'is-top': row.rank != null && row.rank <= 3 }">
                {{ row.rank ?? '—' }}
              </td>
              <td>
                <div class="cell-main">
                  <span class="cell-icon"><Icon name="sun" :size="15" /></span>
                  <div class="cell-stack">
                    <span class="cell-name">{{ row.name || row.device_code }}</span>
                    <span class="cell-note num">{{ row.device_code }}</span>
                  </div>
                </div>
              </td>
              <td>
                <div class="score-cell">
                  <span class="meter" :class="`t-${scoreTone(row.score)}`">
                    <i :style="{ width: `${row.score ?? 0}%` }"></i>
                  </span>
                  <b class="num score" :class="`s-${scoreTone(row.score)}`">
                    {{ row.score == null ? '—' : n(row.score, 0) }}
                  </b>
                </div>
              </td>
              <td><StatusChip :tone="chipTone(row.state)" :label="HEALTH_STATE_CN[row.state] || row.state" /></td>
              <td>
                <div class="basis-cell" :title="healthBasis(row.matched_by)">
                  <span class="basis-text">{{ healthBasisShort(row.matched_by) }}</span>
                  <span v-if="row.active_level" class="basis-level">{{ LEVEL_CN[row.active_level] || row.active_level }}</span>
                </div>
              </td>
              <td class="c num clock-cell">{{ clockMs(row.sampled_at_ms) }}</td>
            </tr>
            <tr v-if="!paged.length && !loading">
              <td colspan="6" class="empty">没有符合条件的设备</td>
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
        :note="`每页 ${pageSize} 台`"
      />
    </section>

    <aside class="hl-side">
      <section class="card">
        <div class="card-head">
          <span class="card-title">场站健康</span>
          <span class="card-sub">按均分排名</span>
        </div>
        <div class="station-list">
          <div v-for="item in stationRows" :key="item.code" class="station-row">
            <div class="station-head">
              <span class="station-name">{{ item.name }}</span>
              <b class="num" :class="`s-${scoreTone(item.score)}`">
                {{ item.score == null ? '—' : n(item.score, 1) }}
              </b>
            </div>
            <span class="meter" :class="`t-${scoreTone(item.score)}`">
              <i :style="{ width: `${item.score ?? 0}%` }"></i>
            </span>
            <span class="station-note num">
              {{ item.valid }} / {{ item.total }} 台有效<template v-if="item.provisional"> · 暂定分（有未知设备）</template>
            </span>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">评分档位</span>
          <span class="card-sub">rule-health-v1 只有五档</span>
        </div>
        <div class="band-body">
          <div class="band-bar">
            <i
              v-for="band in bands"
              :key="band.score"
              :style="{ width: `${band.share}%`, background: band.color }"
              :title="`${band.score} 分 · ${band.label} ${band.count} 台`"
            />
          </div>
          <div class="band-legend">
            <span v-for="band in bands" :key="band.score">
              <i :style="{ background: band.color }"></i>{{ band.score }} 分 · {{ band.label }}
              <b class="num">{{ band.count }}</b>
            </span>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <span class="card-title">计算口径</span>
        </div>
        <div class="note-body">
          <p>规则版本 <b class="num">{{ board?.rule_version || '—' }}</b>，计算于 {{ stamp(board?.computed_at) }}。</p>
          <p>分数只统计有有效遥测的设备；无采样的设备记为「未知」，不进排名也不拉低场站均分。</p>
          <p>数据完整：{{ board?.complete ? '是' : '否（部分设备缺采样）' }}。</p>
        </div>
      </section>
    </aside>
  </div>

  <Drawer
    v-if="selected"
    :title="selected.name || selected.device_code"
    :sub="`${selected.device_code} · ${stationName.get(selected.station_code) || selected.station_code}`"
    @close="closeDrawer"
  >
    <div class="drawer-score">
      <DonutGauge
        :value="selected.score ?? 0"
        :tone="scoreTone(selected.score) === 'bad' ? 'bad' : scoreTone(selected.score) === 'ok' ? 'ok' : 'brand'"
        :label="HEALTH_STATE_CN[selected.state] || selected.state"
        :note="`第 ${selected.rank ?? '—'} 名`"
      />
      <div class="drawer-facts">
        <div class="fact">
          <span>健康分</span>
          <b class="num">{{ selected.score == null ? '—' : n(selected.score, 1) }}</b>
        </div>
        <div class="fact">
          <span>排名</span>
          <b class="num">{{ selected.rank == null ? '—' : `第 ${selected.rank} 名` }}</b>
        </div>
        <div class="fact">
          <span>最新遥测</span>
          <b>{{ selected.status == null ? '无采样' : TELEMETRY_STATE_CN[selected.status] || selected.status }}</b>
        </div>
        <div class="fact">
          <span>场站</span>
          <b>{{ selected.station_code }}</b>
        </div>
      </div>
    </div>

    <dl class="kv">
      <dt>设备编码</dt>
      <dd class="num">{{ selected.device_code }}</dd>
      <dt>设备名称</dt>
      <dd>{{ selected.name || '—' }}</dd>
      <dt>评分依据</dt>
      <dd>{{ healthBasis(selected.matched_by) }}</dd>
      <template v-if="selected.active_level">
        <dt>最高活动告警</dt>
        <dd>{{ LEVEL_CN[selected.active_level] || selected.active_level }}</dd>
      </template>
      <dt>采样时间</dt>
      <dd class="num">{{ clockMs(selected.sampled_at_ms) }}</dd>
      <dt>接收时间</dt>
      <dd class="num">{{ selected.received_at ? stamp(selected.received_at) : '—' }}</dd>
      <dt>规则版本</dt>
      <dd class="num">{{ board?.rule_version || '—' }}</dd>
    </dl>

    <div class="panel-note">
      健康分是规则引擎按温度、电压波动、功率衰减等因子加权算出的 0–100 分，不等于剩余寿命，
      也不代表可用于结算的可用率。
    </div>

    <template #footer>
      <RouterLink class="btn" :to="{ path: '/devices', query: { q: selected.device_code } }">
        在设备台账中查看<Icon name="chevronRight" :size="14" />
      </RouterLink>
      <RouterLink class="btn btn-primary" :to="{ path: '/predictions', query: { q: selected.device_code } }">
        看故障预测<Icon name="arrowRight" :size="14" />
      </RouterLink>
    </template>
  </Drawer>
</template>

<style scoped>
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

.hl-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 322px;
  gap: 16px;
  /* 左栏卡片拉到与右栏等高，底边压在同一条线上；分页条靠 margin-top:auto 沉底 */
  align-items: stretch;
}

.hl-grid > section.card {
  display: flex;
  flex-direction: column;
}

.hl-grid > section.card > .pager {
  margin-top: auto;
}

.hl-side {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px 14px;
}

.hl-table {
  table-layout: fixed;
  min-width: 860px;
}

.hl-table .c {
  text-align: center;
}

.rank {
  font-weight: 700;
  color: var(--ink-3);
}

.rank.is-top {
  color: var(--brand);
}

/* 评分依据：一句话解释「这个分怎么来的」，级别单独一枚小标，不占正文宽度 */
.basis-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.basis-text {
  font-size: 12px;
  color: var(--ink-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.basis-level {
  flex: 0 0 auto;
  padding: 0 6px;
  height: 18px;
  line-height: 18px;
  border-radius: var(--r-sm);
  background: var(--warn-soft);
  color: var(--warn-deep);
  font-size: 10.5px;
  font-weight: 600;
}

.clock-cell {
  font-size: 12px;
  color: var(--ink-2);
}

.score-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.score-cell .meter {
  flex: 1;
  min-width: 0;
  max-width: 110px;
}

.score {
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
  min-width: 34px;
  text-align: right;
}

.s-ok {
  color: var(--ok-deep);
}

.s-bad {
  color: var(--bad);
}

.station-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 20px 18px;
}

.station-row {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.station-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.station-name {
  font-size: 12.5px;
  color: var(--ink-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.station-head b {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--ink);
}

.station-note {
  font-size: 10.5px;
  color: var(--ink-4);
}

/* 档位条：五档是离散值，用一条拼接的色带比五根独立柱子更省地方，也看得出比例 */
.band-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 2px 20px 18px;
}

.band-bar {
  display: flex;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--line-soft);
}

.band-bar i {
  display: block;
  height: 100%;
}

.band-legend {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.band-legend span {
  display: flex;
  align-items: center;
  gap: 6px;
}

.band-legend i {
  width: 7px;
  height: 7px;
  border-radius: 2px;
}

.band-legend b {
  margin-left: auto;
  color: var(--ink);
}

.note-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 20px 18px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.note-body b {
  color: var(--ink-2);
}

.drawer-score {
  display: flex;
  align-items: center;
  gap: 18px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--line-soft);
}

.drawer-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 14px;
  flex: 1;
}

.fact {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.fact span {
  font-size: 11px;
  color: var(--ink-3);
}

.fact b {
  font-size: 13px;
  color: var(--ink);
}

@media (max-width: 1240px) {
  .hl-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .hl-side {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
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

  .hl-side {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
