/**
 * 模型通道余额：全局一份，顶栏小胶囊与模型面板共用。
 *
 * 三条原则：
 * - 只在管理员登录后拉取，后端本来就要求 admin（403 时面板显示无权限，不当作错误弹窗）。
 * - 余额来自供应商的额度接口，频率不能高：顶栏 10 分钟刷一次，打开面板时再刷一次。
 * - 拉取失败保留上一次的值。清零会让顶栏显示一个假的「¥0.00」，比不显示更容易误导。
 */
import { computed, ref } from 'vue'
import { api } from './api'
import type { ModelBalanceBoard, ModelChannelBalance } from './api'

export const modelBoard = ref<ModelBalanceBoard | null>(null)
export const modelLoading = ref(false)
export const modelError = ref('')

const CURRENCY_SIGN: Record<string, string> = { CNY: '¥', USD: '$' }

/** 金额是供应商返回的字符串，不在这里做浮点换算，避免精度失真 */
export function moneyText(entry: { currency: string; total_balance: string } | null | undefined): string {
  if (!entry || !entry.total_balance) return '—'
  const sign = CURRENCY_SIGN[entry.currency] ?? (entry.currency ? `${entry.currency} ` : '')
  return `${sign}${entry.total_balance}`
}

/** 顶栏只放解读通道：自动告警解读是模型调用的主要成本来源；两条通道的明细在面板里 */
export const primaryChannel = computed<ModelChannelBalance | null>(() => {
  const list = modelBoard.value?.channels ?? []
  return list.find((item) => item.channel === 'interpret') ?? list[0] ?? null
})

export const topbarBalance = computed(() => {
  const channel = primaryChannel.value
  return channel ? moneyText(channel.balances?.[0] ?? null) : ''
})

/** 查询失败或余额不足都要在顶栏看得出来，不能只显示一个正常颜色数字 */
export const topbarTone = computed<'ok' | 'warn' | 'bad'>(() => {
  const channel = primaryChannel.value
  if (!channel) return 'warn'
  if (channel.is_available === false) return 'bad'
  if (channel.error) return 'warn'
  return 'ok'
})

let inflight: Promise<void> | null = null

export function refreshModelBoard(): Promise<void> {
  if (inflight) return inflight
  modelLoading.value = true
  inflight = (async () => {
    try {
      modelBoard.value = await api.modelBalance()
      modelError.value = ''
    } catch (error) {
      modelError.value = error instanceof Error ? error.message : String(error)
    } finally {
      modelLoading.value = false
      inflight = null
    }
  })()
  return inflight
}

let timer: number | null = null

export function startModelPolling(intervalMs = 600_000): void {
  stopModelPolling()
  void refreshModelBoard()
  timer = window.setInterval(() => void refreshModelBoard(), intervalMs)
}

export function stopModelPolling(): void {
  if (timer !== null) window.clearInterval(timer)
  timer = null
}

/** 退出登录时必须清掉：余额是账号资产信息，不能留给下一个登录的人 */
export function resetModelBoard(): void {
  stopModelPolling()
  modelBoard.value = null
  modelError.value = ''
}
