<script setup lang="ts">
/**
 * 告警解读：把一条告警翻译成「模型结论 / 关键事实 / 建议动作 / 局限 / 证据快照」。
 *
 * 结论来自真实后端 `/agent/interpretations`（生产跑 v2）：后端 interpretationScanner 扫到新告警建任务，
 * interpretationWorker 调模型后写回 result，所以刚触发的告警会先读到 `task_status=pending`——
 * 这不是「没有解读」，界面必须说「生成中」并等它。
 *
 * 模型没跑通（未配置、额度用尽、熔断、证据不足等）时**不编结论**：退回按指标与级别生成的通用排查顺序，
 * 并在标题上标明「模板，非模型结论」。这套模板是兜底，不是主线。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import Icon from './Icon.vue'
import Drawer from './Drawer.vue'
import { api } from '../lib/api'
import type { Alarm, HealthRow, Interpretation, Prediction } from '../lib/api'
import { across, LEVEL_CN, METRIC_CN, METRIC_DIGITS, METRIC_UNIT, n, pct } from '../lib/format'
import { TASK_STATUS_CN, insightView } from '../lib/interpretation'
import { shell } from '../lib/store'

const emit = defineEmits<{ close: []; read: [] }>()

const list = ref<Alarm[]>([])
const selected = ref<Alarm | null>(null)
const deviceLabel = ref('')
const health = ref<HealthRow | null>(null)
const prediction = ref<Prediction | null>(null)
const deviceAlarmCount = ref(0)
const loading = ref(true)
const failed = ref('')

/* 解读单独一套状态：告警数据取到了、解读还在路上，是很正常的情况 */
const insight = ref<Interpretation | null>(null)
const insightLoading = ref(false)
const insightError = ref('')
const view = computed(() => insightView(insight.value))

/** 成因与动作按指标给模板；级别只决定处置时限，不决定成因。模型不可用时才用这两张表。 */
const CAUSES: Record<string, string[]> = {
  temperature: [
    '组串遮挡或通风受限，逆变器散热风道积灰',
    '高辐照叠加满发工况，触发降额前的温度爬升',
    '温度传感器漂移，读数高于实际壳温',
  ],
  voltage: [
    '组串开路或接线端子松动，直流侧电压被抬高',
    '逆变器 MPPT 工作点偏移，追踪异常',
    '并网点电压偏高，触发过压保护',
  ],
  current: [
    '组串短路或接地故障，支路电流异常',
    '汇流箱熔断器熔断，相邻支路电流归零',
    '电流互感器采样偏差，需与钳形表比对',
  ],
  power: [
    '电网调度限发或 AGC 指令下调出力',
    '逆变器因温度/电压保护自动降载',
    '组串失配或遮挡造成功率损失',
  ],
  ai_failure_risk: [
    '模型识别到部件劣化趋势（风扇、电容、接触器）',
    '近一窗口样本分布漂移，特征值异常',
    '同型号设备出现批量故障前兆',
  ],
}

const ACTIONS: Record<string, string[]> = {
  temperature: ['远程核对同组串相邻设备温度，排除单点传感器问题', '持续 2 个采样周期仍越限，派单现场清灰并检查风扇', '复测未回落则降载运行，避免热失控停机'],
  voltage: ['先查该组串开路电压与绝缘阻抗', '核对并网点电压与无功指令，确认是否越上限', '端子松动按停电检修处理，不在带载状态下紧固'],
  current: ['对比同汇流箱各支路电流，定位异常支路', '检查熔断器与防反二极管，必要时更换', '确认接地故障后按安全规程隔离支路'],
  power: ['核对调度指令与限发曲线，确认是否为计划内限发', '检查逆变器告警与降载记录', '排除遮挡后评估组串失配损失'],
  ai_failure_risk: ['按 top_factors 指出的特征做一次现场巡检', '对比同型号设备同期数据，判断是否批量问题', '风险持续升高再安排停机检修，避免过度响应'],
}

const HOURS: Record<string, string> = { urgent: '15 分钟内响应', major: '2 小时内响应', minor: '当日跟进' }

const load = async (alarm: Alarm) => {
  selected.value = alarm
  health.value = null
  prediction.value = null
  deviceLabel.value = `#${alarm.device_id}`
  void fetchInsight(alarm.id)
  const [deviceResult, healthResult, predictionResult, sameDevice] = await Promise.allSettled([
    api.device(alarm.device_id),
    api.healthDevices(),
    api.prediction(alarm.device_id),
    api.alarms({ deviceId: alarm.device_id, pageSize: 1 }),
  ])
  if (deviceResult.status === 'fulfilled') {
    deviceLabel.value = deviceResult.value.name || deviceResult.value.device_code
  }
  if (healthResult.status === 'fulfilled') {
    health.value = healthResult.value.devices.find((row) => row.device_id === alarm.device_id) || null
  }
  if (predictionResult.status === 'fulfilled') prediction.value = predictionResult.value
  if (sameDevice.status === 'fulfilled') deviceAlarmCount.value = sameDevice.value.total
}

/* ---------------- 解读：取数、等待、标已读 ---------------- */

/**
 * 解读是异步任务：pending/running 时每 4 秒回读一次，最多 6 次（约 24 秒）。
 * WebSocket 收到 interpretation_updated 会立刻再读一次（见下方 watch），轮询是没连上时的兜底。
 */
let seq = 0
let alarmRefreshSeq = 0
let pollTimer: number | undefined
let pollLeft = 0

const stopPoll = () => {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = undefined
}

const schedulePoll = (alarmId: number) => {
  stopPoll()
  if (pollLeft <= 0) return
  pollLeft -= 1
  pollTimer = window.setTimeout(() => void fetchInsight(alarmId, { poll: true }), 4000)
}

const markRead = async (id: number) => {
  try {
    await api.readInterpretation(id)
    emit('read')
  } catch {
    /* 已读失败只是红点不消，不影响读内容，不打扰用户 */
  }
}

const fetchInsight = async (alarmId: number, options: { poll?: boolean } = {}) => {
  const mine = ++seq
  if (!options.poll) {
    stopPoll()
    pollLeft = 6
    insightError.value = ''
    insight.value = null
    insightLoading.value = true
  }
  try {
    const page = await api.interpretations({ alarmId, pageSize: 1 })
    if (mine !== seq) return
    const item = page.list[0] || null
    insight.value = item
    insightError.value = ''
    if (item && !item.read) void markRead(item.id)
    if (item && (item.task_status === 'pending' || item.task_status === 'running')) schedulePoll(alarmId)
    else stopPoll()
  } catch (error) {
    if (mine !== seq) return
    insightError.value = insightErrorText(error)
    stopPoll()
  } finally {
    if (mine === seq) insightLoading.value = false
  }
}

/** 接口没上、网关 404、后端 503 维护，三种都要说清，否则用户会以为「这条告警就是没解读」 */
const insightErrorText = (error: unknown): string => {
  const status = (error as { status?: number }).status
  if (status === 404) return '当前后端没有解读接口，无法读取模型结论'
  if (status === 503) return '解读服务暂不可用，请稍后刷新'
  if (status === 401) return '登录已失效，请重新登录后再看'
  if (status === 0) return '无法连接后端服务'
  return `解读读取失败：${error instanceof Error ? error.message : String(error)}`
}

/* 别的标签页/worker 写了新结果，后端广播 interpretation_updated；只有正在等结果时才回读 */
watch(
  () => shell.insightRevision,
  () => {
    const alarm = selected.value
    if (alarm && (view.value.state === 'pending' || insightLoading.value)) void fetchInsight(alarm.id, { poll: true })
  },
)

// State changes from the web or the server demo share alarmRevision. Refresh
// an open detail too, so acknowledgement/recovery is visible without reopening.
watch(() => shell.alarmRevision, async () => {
  const mine = ++alarmRefreshSeq
  const id = selected.value?.id
  if (!id) { await boot(); return }
  try {
    const fresh = await api.alarm(id)
    if (mine !== alarmRefreshSeq || selected.value?.id !== id) return
    selected.value = fresh
    void fetchInsight(id, { poll: true })
  } catch { /* retain the current detail if the refresh is unavailable */ }
})

onUnmounted(() => {
  seq += 1
  alarmRefreshSeq += 1
  stopPoll()
})

const boot = async () => {
  loading.value = true
  try {
    const payload = await api.alarms({ status: 'unhandled', pageSize: 6 })
    list.value = payload.list
    if (payload.list.length) await load(payload.list[0])
    failed.value = ''
  } catch (error) {
    failed.value = String(error)
  } finally {
    loading.value = false
  }
}

onMounted(boot)

/* ---------------- 展示口径 ---------------- */

const metricLabel = computed(() => METRIC_CN[selected.value?.metric || ''] || selected.value?.metric || '—')
const unit = computed(() => METRIC_UNIT[selected.value?.metric || ''] ?? '')
const digits = computed(() => METRIC_DIGITS[selected.value?.metric || ''] ?? 1)

const overBy = computed(() => {
  const alarm = selected.value
  if (!alarm || !alarm.threshold) return null
  return ((alarm.value - alarm.threshold) / Math.abs(alarm.threshold)) * 100
})

/** 越限幅度映射成进度条：0% 起步，超过 40% 打满 */
const barWidth = computed(() => {
  const value = overBy.value
  if (value == null) return 0
  return Math.max(4, Math.min(100, (value / 40) * 100))
})

const causes = computed(() => CAUSES[selected.value?.metric || ''] || ['该指标暂无固定成因模板，建议结合现场核对'])
const actions = computed(() => ACTIONS[selected.value?.metric || ''] || ['核对设备运行状态与最近一次巡检记录'])
const response = computed(() => HOURS[selected.value?.level || 'minor'] || '当日跟进')

const healthText = computed(() => {
  const row = health.value
  if (!row || row.score == null) return '无有效样本'
  return `${n(row.score, 0)} 分 · 第 ${row.rank ?? '—'} 名`
})

const riskText = computed(() => {
  const risk = prediction.value
  if (!risk || risk.probability == null) return '暂无预测'
  return `${pct(risk.probability * 100, 1)}（阈值 ${pct((risk.threshold ?? 0) * 100, 0)}）`
})

/** 状态胶囊：生成中走中性，已解读走品牌色，未完成/证据不足走告警色 */
const stateLabel = computed(() => {
  if (insightLoading.value) return '读取中'
  if (insightError.value) return '读取失败'
  switch (view.value.state) {
    case 'ready':
      return view.value.fixed ? '固定解读' : '已解读'
    case 'pending':
      return TASK_STATUS_CN[insight.value?.task_status || 'pending'] || '生成中'
    case 'degraded':
      return view.value.refused ? '证据不足' : '未完成'
    default:
      return '未生成'
  }
})

const stateTone = computed(() => {
  if (insightLoading.value || insightError.value) return 'is-plain'
  if (view.value.state === 'ready') return 'is-brand'
  if (view.value.state === 'degraded') return 'is-warn'
  return 'is-plain'
})

/** 副行：谁给的结论、什么时候给的、走的是哪版协议 */
const metaLine = computed(() => {
  const item = insight.value
  if (!item) return ''
  const parts: string[] = []
  if (view.value.state === 'ready') parts.push(view.value.fixed ? '固定模板 · 未调用API' : view.value.model || '模型')
  parts.push(`#${item.id}`)
  /* pending 时 updated_at 是上一次写入的时间，显示出来会被读成「已经等了这么久」，等结果时先不显示 */
  if (item.updated_at && view.value.state !== 'pending') parts.push(across(item.updated_at))
  if (view.value.protocol) parts.push(`协议 v${view.value.protocol}`)
  if (!item.active) parts.push('事件已恢复')
  return parts.join(' · ')
})

/** 事实只铺一屏：device_alarm 的 facts 可能十几条，全铺会把建议动作顶出视野 */
const shownFacts = computed(() => view.value.facts.slice(0, 8))
const restFacts = computed(() => Math.max(0, view.value.factTotal - shownFacts.value.length))
</script>

<template>
  <Drawer title="告警解读" sub="解读说明、关键事实与核查建议，固定模板和模型结果分别标明来源" @close="emit('close')">
    <div v-if="failed" class="error-box">告警未取到：{{ failed }}</div>

    <template v-else-if="loading">
      <p class="loading">读取未处理告警…</p>
    </template>

    <template v-else-if="!selected">
      <p class="empty">当前没有未处理告警，不需要解读。</p>
    </template>

    <template v-else>
      <!-- 切换：同一屏内换一条告警 -->
      <div class="switch">
        <button
          v-for="item in list"
          :key="item.id"
          class="switch-item"
          :class="{ 'is-on': item.id === selected.id }"
          @click="load(item)"
        >
          <span class="sev" :class="`sev-${item.level}`"></span>
          <span class="switch-text">{{ METRIC_CN[item.metric] || item.metric }} · {{ LEVEL_CN[item.level] }}</span>
          <span class="switch-time num">{{ across(item.triggered_at) }}</span>
        </button>
      </div>

      <header class="insight-head">
        <div class="insight-title">
          <span class="chip" :class="selected.level === 'urgent' ? 'is-bad' : selected.level === 'major' ? 'is-warn' : 'is-plain'">
            <i></i>{{ LEVEL_CN[selected.level] }}
          </span>
          <strong>{{ deviceLabel }}</strong>
        </div>
        <p class="insight-sub">
          {{ metricLabel }}越限 · 告警 #{{ selected.id }} · 触发于 {{ across(selected.triggered_at) }} · {{ response }}
        </p>
      </header>

      <section class="read-block">
        <div class="read-row">
          <div class="read-cell">
            <span>实测值</span>
            <b class="num">{{ n(selected.value, digits) }}<em>{{ unit }}</em></b>
          </div>
          <div class="read-cell">
            <span>阈值</span>
            <b class="num">{{ n(selected.threshold, digits) }}<em>{{ unit }}</em></b>
          </div>
          <div class="read-cell">
            <span>越限</span>
            <b class="num" :class="overBy && overBy > 0 ? 'is-over' : ''">
              {{ overBy == null ? '—' : `${overBy > 0 ? '+' : ''}${n(overBy, 1)}%` }}
            </b>
          </div>
        </div>
        <div class="read-bar">
          <i :style="{ width: `${barWidth}%` }"></i>
        </div>
        <p class="read-note">
          判据 <code>{{ selected.rule_operator }}</code> · 规则版本 {{ selected.rule_version }}
        </p>
      </section>

      <!-- 解读状态：生成中 / 已解读 / 未完成 / 未生成，四种都要能一眼分清 -->
      <div class="insight-bar">
        <span class="chip" :class="stateTone"><i></i>{{ stateLabel }}</span>
        <span class="insight-meta">{{ metaLine }}</span>
        <button class="link-btn" @click="fetchInsight(selected.id)">重新读取</button>
      </div>
      <p v-if="insightError" class="insight-note is-bad">{{ insightError }}</p>
      <p v-else class="insight-note">{{ view.note }}</p>

      <div v-if="insightLoading && !insight" class="insight-skeleton">
        <span></span><span></span><span></span>
      </div>

      <!-- 模型给了结论才铺这几块；没给就用下面的模板兜底 -->
      <template v-if="view.state === 'ready'">
        <section v-if="view.conclusion.length" class="block">
          <h3>解读结论</h3>
          <p v-for="line in view.conclusion" :key="line" class="conclusion">{{ line }}</p>
        </section>

        <section v-if="shownFacts.length" class="block">
          <h3>关键事实<span class="h3-note">取自证据快照，非模型生成</span></h3>
          <dl class="fact-grid">
            <div v-for="fact in shownFacts" :key="fact.label + fact.value" class="fact" :class="{ 'is-wide': fact.wide }">
              <dt>{{ fact.label }}</dt>
              <dd class="num">{{ fact.value }}<em v-if="fact.unit">{{ fact.unit }}</em></dd>
            </div>
          </dl>
          <p v-if="restFacts" class="h3-note">另有 {{ restFacts }} 条事实未展开</p>
        </section>

        <section v-if="view.suggestions.length" class="block">
          <h3>建议动作</h3>
          <ol class="list is-action">
            <li v-for="(item, index) in view.suggestions" :key="item">
              <span class="idx">{{ index + 1 }}</span>
              <span>{{ item }}</span>
            </li>
          </ol>
        </section>

        <section v-if="view.limitations.length" class="block">
          <h3>尚未解释的部分</h3>
          <ul class="limits">
            <li v-for="item in view.limitations" :key="item">{{ item }}</li>
          </ul>
        </section>

        <section v-if="view.evidence.length" class="block">
          <h3>证据快照</h3>
          <div class="ev-list">
            <div v-for="item in view.evidence" :key="item.id" class="ev">
              <span class="ev-id num">{{ item.id }}</span>
              <span class="ev-tool">{{ item.tool }}</span>
              <span class="ev-status" :class="item.status === 'ok' ? '' : 'is-warn'">{{ item.status }}</span>
              <span class="ev-at num">{{ item.at ? across(item.at) : '—' }}</span>
            </div>
          </div>
        </section>
      </template>

      <!-- 兜底：模型没给结论时，按指标与级别的通用顺序，明确标注不是模型结论 -->
      <section v-else-if="!insightLoading" class="block fallback">
        <h3>通用排查顺序<span class="h3-note">按指标与级别生成的模板，非模型结论</span></h3>
        <p class="fb-label">可能成因</p>
        <ol class="list">
          <li v-for="(item, index) in causes" :key="item">
            <span class="idx">{{ index + 1 }}</span>
            <span>{{ item }}</span>
          </li>
        </ol>
        <p class="fb-label">建议动作</p>
        <ol class="list is-action">
          <li v-for="(item, index) in actions" :key="item">
            <span class="idx">{{ index + 1 }}</span>
            <span>{{ item }}</span>
          </li>
        </ol>
      </section>

      <section class="block">
        <h3>旁证</h3>
        <dl class="kv compact">
          <dt>设备健康分</dt>
          <dd>{{ healthText }}</dd>
          <dt>该设备告警</dt>
          <dd class="num">{{ deviceAlarmCount }} 条未归档</dd>
          <dt>失效风险</dt>
          <dd class="num">{{ riskText }}</dd>
          <dt>告警来源</dt>
          <dd>{{ selected.origin === 'simulation' ? '仿真推演' : '现场采集' }}</dd>
        </dl>
      </section>

      <p class="panel-note">
        {{ view.fixed ? '当前使用固定规则模板，未调用模型API。' : '模型分析基于事件证据快照。' }}解读只用于核查参考，不是故障结论；读数取自告警触发时刻，现场复测后以设备实测为准。
      </p>
    </template>

    <template #footer>
      <RouterLink v-if="selected" class="btn" :to="{ path: '/alarms', query: { id: String(selected.id) } }" @click="emit('close')">
        打开告警详情<Icon name="chevronRight" :size="14" />
      </RouterLink>
      <RouterLink v-if="selected" class="btn" :to="`/devices/${selected.device_id}`" @click="emit('close')">
        设备档案
      </RouterLink>
      <span class="grow"></span>
      <button class="btn btn-primary" @click="emit('close')">知道了</button>
    </template>
  </Drawer>
</template>

<style scoped>
/* 两列固定等宽。原来是 flex + inline-flex，每格宽度跟着文字走，
   「3 分前」和「7 时 29 分前」会把同一行撑成两种宽度，竖着看就错位。
   现在列宽由网格统一，时间用 margin-left:auto 推到右端形成一条竖线。 */
.switch {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin-bottom: 14px;
}

.switch-item {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  height: 30px;
  padding: 0 10px;
  border-radius: var(--r-sm);
  border: 1px solid var(--line);
  background: #fff;
  font-size: 11.5px;
  color: var(--ink-3);
  text-align: left;
}

.switch-item:hover {
  border-color: var(--line-strong);
  color: var(--ink);
}

.switch-item.is-on {
  border-color: var(--brand-line);
  background: var(--brand-soft);
  color: var(--brand-deep);
}

/* 文字先截断，时间永远完整——否则长指标名会把时间挤出格子 */
.switch-text {
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.switch-time {
  flex: 0 0 auto;
  margin-left: auto;
  padding-left: 6px;
  color: var(--ink-4);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.switch-item.is-on .switch-time {
  color: var(--brand-deep);
}

.sev {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: 0 0 6px;
  background: #94a3b8;
}

.sev-urgent {
  background: var(--bad);
}

.sev-major {
  background: var(--warn);
}

.insight-head {
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line-soft);
}

.insight-title {
  display: flex;
  align-items: center;
  gap: 9px;
}

.insight-title strong {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.insight-sub {
  margin-top: 6px;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--ink-3);
}

.read-block {
  margin-top: 14px;
  padding: 12px 13px 11px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--card-soft);
}

.read-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.read-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.read-cell span {
  font-size: 11px;
  color: var(--ink-3);
}

.read-cell b {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--ink);
}

.read-cell b em {
  font-style: normal;
  font-size: 11px;
  font-weight: 500;
  color: var(--ink-3);
  margin-left: 3px;
}

.read-cell b.is-over {
  color: var(--bad);
}

.read-bar {
  height: 5px;
  margin-top: 10px;
  border-radius: 3px;
  background: #e6ebf3;
  overflow: hidden;
}

.read-bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--bad);
}

.read-note {
  margin-top: 7px;
  font-size: 10.5px;
  color: var(--ink-4);
}

.read-note code {
  font-family: var(--font);
  padding: 1px 4px;
  border-radius: 3px;
  background: #eef1f7;
  color: var(--ink-3);
}

.block {
  margin-top: 16px;
}

.block h3 {
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
  margin-bottom: 8px;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink-2);
}

.list li {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.list .idx {
  flex: 0 0 17px;
  height: 17px;
  margin-top: 1px;
  border-radius: 4px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand-deep);
  font-size: 10.5px;
  font-weight: 600;
}

.list.is-action .idx {
  background: #eef1f7;
  color: var(--ink-3);
}

.kv.compact {
  grid-template-columns: 84px minmax(0, 1fr);
  gap: 7px 12px;
  font-size: 12px;
}

/* ---------------- 解读块（模型结论 / 事实 / 证据） ---------------- */

/* 状态条：胶囊在左、来源信息居中、重新读取贴右；三者同一行，窄屏时信息先截断 */
.insight-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
}

.insight-meta {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 10.5px;
  color: var(--ink-4);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}

.link-btn {
  flex: 0 0 auto;
  padding: 0;
  border: none;
  background: none;
  font-size: 11px;
  color: var(--brand);
  white-space: nowrap;
}

.link-btn:hover {
  color: var(--brand-deep);
  text-decoration: underline;
}

.insight-note {
  margin-top: 6px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.insight-note.is-bad {
  color: var(--bad);
}

/* 读取中的骨架条：不写「加载中」三个字，避免和状态胶囊重复 */
.insight-skeleton {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 12px;
}

.insight-skeleton span {
  height: 9px;
  border-radius: 4px;
  background: linear-gradient(90deg, #eef1f7 0%, #f6f8fc 50%, #eef1f7 100%);
}

.insight-skeleton span:nth-child(2) {
  width: 86%;
}

.insight-skeleton span:nth-child(3) {
  width: 62%;
}

.block h3 .h3-note {
  margin-left: 8px;
  font-size: 10.5px;
  font-weight: 400;
  color: var(--ink-4);
}

/* 结论是按句分条的，行距要松一点，否则三句连排看着像一段没断句 */
.conclusion {
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ink-2);
}

.conclusion + .conclusion {
  margin-top: 5px;
}

.fact-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px;
}

/* 事实格不加边框：一屏 8 个框会把面板切碎，只靠标签/数值的层级区分 */
.fact.is-wide {
  grid-column: 1 / -1;
}

.fact dt {
  font-size: 10.5px;
  color: var(--ink-4);
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.fact dd {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fact dd em {
  font-style: normal;
  font-size: 10.5px;
  font-weight: 500;
  color: var(--ink-4);
  margin-left: 2px;
}

/* 局限用弱化的点列表：它是「还没解释清楚」的部分，不该和建议动作抢注意力 */
.limits {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.limits li {
  position: relative;
  padding-left: 11px;
}

.limits li::before {
  content: '';
  position: absolute;
  left: 2px;
  top: 8px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--ink-4);
}

.ev-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
}

.ev {
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr) 34px auto;
  gap: 8px;
  align-items: center;
  padding: 5px 8px;
  border-radius: var(--r-sm);
  background: var(--card-soft);
  color: var(--ink-3);
}

.ev-id {
  font-weight: 600;
  color: var(--brand-deep);
}

.ev-tool {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ev-status {
  text-align: center;
  border-radius: 4px;
  background: #eaf7ee;
  color: #0f7b34;
}

.ev-status.is-warn {
  background: #fff3e6;
  color: #a35b00;
}

.ev-at {
  color: var(--ink-4);
  white-space: nowrap;
}

/* 兜底块：虚线边 + 浅底，一眼看出「这不是模型说的话」 */
.fallback {
  padding: 12px 13px;
  border: 1px dashed var(--line-strong);
  border-radius: var(--r-md);
  background: var(--card-soft);
}

.fallback h3 {
  margin-bottom: 9px;
}

.fb-label {
  margin: 10px 0 6px;
  font-size: 10.5px;
  color: var(--ink-3);
}

.fallback h3 + .fb-label {
  margin-top: 0;
}

.fallback .list + .list {
  margin-top: 10px;
}

</style>
