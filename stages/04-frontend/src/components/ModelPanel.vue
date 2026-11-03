<script setup lang="ts">
/**
 * 模型与额度：按**功能**逐条展示，而不是按通道合并。
 *
 * 上一版只有两张通道卡，页面上看不到安全审查、运维诊断、质量解读也在烧同一个账户的钱；
 * 余额见底时判断不出会先影响谁。现在改成每个功能一条：模型、地址、余额、状态各自成行，
 * 共用同一把 Key 的功能也各占一条，只是金额相同。
 *
 * 这里刻意不做的事：不提供修改 Key 与模型的入口。Key 是 docker secret 文件，
 * 服务在启动时读一次（Config 是 frozen），网页保存无法热生效；做成可点却要重建容器，
 * 会让人以为改成功了。换模型属于另一条写路径，需要单独设计。
 */
import { computed, ref } from 'vue'
import Drawer from './Drawer.vue'
import Icon from './Icon.vue'
import StatusChip from './StatusChip.vue'
import { api } from '../lib/api'
import type { ModelBalanceEntry, ModelChannelBalance } from '../lib/api'
import { MODEL_FEATURES } from '../lib/model-features'
import type { ModelFeature } from '../lib/model-features'
import { moneyText, modelBoard, modelError, modelLoading, refreshModelBoard } from '../lib/model-usage'
import { askConfirm, toast } from '../lib/toast'
import { clock } from '../lib/format'

const emit = defineEmits<{ close: [] }>()

const probing = ref(false)

const channels = computed<ModelChannelBalance[]>(() => modelBoard.value?.channels ?? [])

const ERROR_CN: Record<string, string> = {
  unsupported_provider: '这条通道不提供余额查询：OpenAI 兼容协议没有额度端点，只有 DeepSeek 官方有',
  missing_key: '这条通道没有读到 API Key',
  auth_failed: 'API Key 无效或已被撤销',
  timeout: '查询超时，供应商没有及时响应',
  unreachable: '连不上供应商',
  invalid_response: '供应商返回的数据无法解析',
  response_too_large: '供应商返回的数据超出上限',
  unavailable: '该服务未启动或未启用',
  not_configured: '这条通道未配置',
}

const errorText = (channel: ModelChannelBalance): string => {
  if (!channel.error) return ''
  if (channel.error.startsWith('http_')) return `供应商返回 HTTP ${channel.error.slice(5)}`
  return ERROR_CN[channel.error] || channel.error
}

/** 状态全部继承自该功能所属的通道；没有通道就等于平台看不到它，不做乐观假设 */
const stateOf = (channel: ModelChannelBalance | null): { tone: string; label: string } => {
  if (!channel) return { tone: 'idle', label: '未接入' }
  if (channel.error === 'not_configured') return { tone: 'idle', label: '未配置' }
  if (channel.error) return { tone: 'warn', label: '查询失败' }
  if (channel.is_available === false) return { tone: 'bad', label: '余额不足' }
  if (channel.is_available === true) return { tone: 'ok', label: '可用' }
  return { tone: 'idle', label: '未知' }
}

interface FeatureRow {
  feature: ModelFeature
  channel: ModelChannelBalance | null
  tone: string
  label: string
  balance: ModelBalanceEntry | null
  error: string
}

/** 一次算完，模板里不再重复查找通道，避免同一份数据在几处各判一遍 */
const rows = computed<FeatureRow[]>(() =>
  MODEL_FEATURES.map((feature) => {
    const channel = feature.channel
      ? channels.value.find((item) => item.channel === feature.channel) ?? null
      : null
    const state = stateOf(channel)
    return {
      feature,
      channel,
      tone: state.tone,
      label: state.label,
      balance: channel?.balances?.[0] ?? null,
      error: channel ? errorText(channel) : '',
    }
  }),
)

const found = computed(() => rows.value.filter((row) => row.channel).length)

const signOf = (currency: string): string => {
  if (currency === 'CNY') return '¥'
  if (currency === 'USD') return '$'
  return currency ? `${currency} ` : ''
}

const detailOf = (entry: ModelBalanceEntry): string =>
  `赠送 ${signOf(entry.currency)}${entry.granted_balance || '0.00'} · 充值 ${signOf(entry.currency)}${entry.topped_up_balance || '0.00'}`

const runProbe = async () => {
  if (probing.value) return
  const go = await askConfirm({
    title: '测试解读通道',
    body: '会真实调用一次模型并跑完整输出校验（总预算 40 秒），后端限制每分钟一次。通过后会清除熔断标记，并把最近 24 小时内因未配置、认证失败或额度用尽而失败的解读重新排队。这条测试只针对告警解读，不影响其它功能。',
    confirmText: '开始测试',
  })
  if (!go) return
  probing.value = true
  try {
    const result = await api.modelProbe()
    toast('ok', `解读通道可用${result.model ? `（${result.model}）` : ''}`)
    await refreshModelBoard()
  } catch (error) {
    toast('bad', `测试未通过：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    probing.value = false
  }
}
</script>

<template>
  <Drawer title="模型与额度" sub="按功能逐条看，账目仍归各自的 Key" @close="emit('close')">
    <div v-if="modelError" class="mp-error">余额未取到：{{ modelError }}</div>

    <p class="mp-note">
      这里按「谁在用模型」分条列出：同一个账号下的功能就算共用一把 Key，也各占一条，余额见底时能直接看出影响范围。
      余额是<b>账户级</b>的，不是按 Key 分账——两条通道金额相同，通常只是同一个供应商账号的两把 Key。
      金额来自供应商的额度接口，不是本平台的用量统计。
      <template v-if="!modelError && rows.length">
        已取到 {{ found }}/{{ rows.length }} 项。
      </template>
    </p>

    <article v-for="row in rows" :key="row.feature.key" class="mp-card">
      <div class="mp-head">
        <div class="mp-title">
          <strong>{{ row.feature.name }}</strong>
          <span class="mp-service">{{ row.feature.service }}</span>
        </div>
        <StatusChip :tone="row.tone" :label="row.label" />
      </div>

      <p class="mp-purpose">{{ row.feature.purpose }}</p>

      <dl class="kv">
        <dt>结算</dt>
        <dd>{{ row.feature.keyOwner }}</dd>
        <dt>模型</dt>
        <dd>{{ row.channel?.model || '—' }}</dd>
        <dt>地址</dt>
        <dd class="mp-url">{{ row.channel?.base_url || '—' }}</dd>
        <dt>余额</dt>
        <dd>
          <template v-if="row.balance">
            <b class="num mp-amount">{{ moneyText(row.balance) }}</b>
            <span class="mp-sub">{{ detailOf(row.balance) }}</span>
          </template>
          <span v-else class="mp-none">—</span>
        </dd>
      </dl>

      <p v-if="row.error" class="mp-warn">{{ row.error }}</p>
      <p v-else-if="row.channel && row.channel.is_available === false" class="mp-warn">
        供应商表示余额不足，后续调用会被拒绝（HTTP 402）。
      </p>
      <p v-else-if="row.channel && !row.channel.balances.length" class="mp-warn">
        接口通了，但没有返回余额条目。
      </p>
      <p v-else-if="row.feature.probeNote || !row.channel" class="mp-muted">
        {{ row.feature.probeNote || '平台没有这项的余额查询。' }}
      </p>

      <div class="mp-foot">
        <span class="mp-stamp">
          {{ row.channel?.fetched_at ? `查询于 ${clock(row.channel.fetched_at)}` : '没有可查的账目' }}
        </span>
        <button v-if="row.feature.probe" class="btn btn-sm" :disabled="probing" @click="runProbe">
          {{ probing ? '测试中…' : '测试连接' }}
        </button>
        <span v-else class="mp-probe-off">无独立测试</span>
      </div>
    </article>

    <template #footer>
      <button class="btn" :disabled="modelLoading" @click="refreshModelBoard">
        <Icon name="refresh" :size="15" />刷新余额
      </button>
    </template>
  </Drawer>
</template>

<style scoped>
.mp-note {
  margin: 0 0 14px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink-3);
}

.mp-error {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--bad-soft);
  border: 1px solid #f6cccc;
  color: var(--bad-deep);
  font-size: 12px;
}

.mp-card {
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 14px;
  margin-bottom: 12px;
  background: var(--card);
}

.mp-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.mp-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  flex-wrap: wrap;
}

.mp-title strong {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--ink);
}

.mp-service {
  font-size: 10.5px;
  color: var(--ink-4);
  padding: 1px 6px;
  border: 1px solid var(--line);
  border-radius: var(--r-pill);
}

.mp-purpose {
  margin: 6px 0 12px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.mp-url {
  font-size: 11.5px;
}

.mp-amount {
  font-size: 15px;
  font-weight: 700;
  color: var(--ink);
}

.mp-sub {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  color: var(--ink-4);
}

.mp-none {
  color: var(--ink-4);
}

.mp-warn {
  margin-top: 10px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--warn-deep);
}

/* 未接入不是故障，别用警告色，否则三项都像报错 */
.mp-muted {
  margin-top: 10px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-4);
}

.mp-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 10px;
}

.mp-stamp {
  font-size: 10.5px;
  color: var(--ink-4);
}

.mp-probe-off {
  font-size: 10.5px;
  color: var(--ink-4);
}
</style>
