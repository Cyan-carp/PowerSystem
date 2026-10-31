<script setup lang="ts">
/**
 * 模型通道面板：只读展示两条通道的模型、地址与供应商余额，外加一次「测试连接」。
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
  unavailable: '智能体服务未启动或未启用',
  not_configured: '这条通道未配置',
}

const errorText = (channel: ModelChannelBalance): string => {
  if (!channel.error) return ''
  if (channel.error.startsWith('http_')) return `供应商返回 HTTP ${channel.error.slice(5)}`
  return ERROR_CN[channel.error] || channel.error
}

const toneOf = (channel: ModelChannelBalance): string => {
  if (channel.error) return 'warn'
  if (channel.is_available === false) return 'bad'
  if (channel.is_available === true) return 'ok'
  return 'idle'
}

const labelOf = (channel: ModelChannelBalance): string => {
  if (channel.error) return '异常'
  if (channel.is_available === false) return '余额不足'
  if (channel.is_available === true) return '可用'
  return '未知'
}

const signOf = (currency: string): string => {
  if (currency === 'CNY') return '¥'
  if (currency === 'USD') return '$'
  return currency ? `${currency} ` : ''
}

const detailOf = (entry: ModelBalanceEntry): string =>
  `赠送 ${signOf(entry.currency)}${entry.granted_balance || '0.00'} · 充值 ${signOf(entry.currency)}${entry.topped_up_balance || '0.00'}`

const probe = async () => {
  if (probing.value) return
  const go = await askConfirm({
    title: '测试解读通道',
    body: '会真实调用一次模型并跑完整输出校验（总预算 40 秒），后端限制每分钟一次。通过后会清除熔断标记，并把最近 24 小时内因未配置、认证失败或额度用尽而失败的解读重新排队。这条测试只针对告警解读通道，不影响主助手。',
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
  <Drawer title="模型与额度" sub="每条通道用自己的 Key 查自己的账" @close="emit('close')">
    <div v-if="modelError" class="mp-error">通道信息未取到：{{ modelError }}</div>

    <p class="mp-note">
      余额来自供应商的额度接口，不是本平台的用量统计。金额以供应商返回为准，本页不做汇率换算。
    </p>

    <section v-for="channel in channels" :key="channel.channel" class="mp-card">
      <div class="mp-head">
        <div class="mp-title">
          <strong>{{ channel.label || channel.channel }}</strong>
          <span class="mp-channel">{{ channel.channel }}</span>
        </div>
        <StatusChip :tone="toneOf(channel)" :label="labelOf(channel)" />
      </div>

      <dl class="kv">
        <dt>模型</dt>
        <dd>{{ channel.model || '—' }}</dd>
        <dt>地址</dt>
        <dd class="mp-url">{{ channel.base_url || '—' }}</dd>
        <template v-for="(entry, index) in channel.balances" :key="entry.currency || index">
          <dt>{{ entry.currency || '余额' }}</dt>
          <dd>
            <b class="num mp-amount">{{ moneyText(entry) }}</b>
            <span class="mp-sub">{{ detailOf(entry) }}</span>
          </dd>
        </template>
      </dl>

      <p v-if="channel.error" class="mp-warn">{{ errorText(channel) }}</p>
      <p v-else-if="channel.is_available === false" class="mp-warn">
        供应商表示余额不足，后续调用会被拒绝（HTTP 402）。
      </p>
      <p v-else-if="!channel.balances.length" class="mp-warn">接口通了，但没有返回余额条目。</p>

      <p class="mp-stamp">查询于 {{ channel.fetched_at ? clock(channel.fetched_at) : '—' }}</p>
    </section>

    <div v-if="!channels.length && !modelLoading" class="mp-empty">还没有取到通道信息。</div>

    <template #footer>
      <button class="btn" :disabled="modelLoading" @click="refreshModelBoard">
        <Icon name="refresh" :size="15" />刷新余额
      </button>
      <button class="btn" :disabled="probing" @click="probe">
        {{ probing ? '测试中…' : '测试解读通道' }}
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
  margin-bottom: 12px;
}

.mp-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.mp-title strong {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--ink);
}

.mp-channel {
  font-size: 11px;
  color: var(--ink-4);
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

.mp-warn {
  margin-top: 10px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--warn-deep);
}

.mp-stamp {
  margin-top: 8px;
  font-size: 10.5px;
  color: var(--ink-4);
}

.mp-empty {
  padding: 18px 0;
  text-align: center;
  font-size: 12px;
  color: var(--ink-4);
}
</style>
