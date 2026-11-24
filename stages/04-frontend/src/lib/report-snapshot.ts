/**
 * 报表默认快照：全站一份，只负责「场站总览 → 统计报表」的一次性交接。
 *
 * 为什么需要它：场站总览挂载时本来就要算一次默认窗口（最近 7 天、全部设备）的可用率报表
 * 来画数据质量卡。统计报表的默认查询与它同口径，把这份结果留下来就能省掉一次
 * 143 台 × 7 天的计算——不需要新增任何预加载请求。
 *
 * 刻意保持窄：
 * - 只存一份，不按查询条件建索引、不做 LRU、不做超时清理、不做在飞复用；
 * - 取走即清空：这是交接，不是缓存；
 * - 归属凭 token + 用户 id + 角色判定，任一变化即作废；
 * - 不落 localStorage / sessionStorage，刷新页面即失效，不在磁盘上留上一个账号的数据；
 * - 超龄不丢弃：按业务规范，超龄快照照常展示，只在界面上提示手动刷新。
 *
 * 它只保护「交接」这一段。页面内数据的安全边界靠组件生命周期：登出与 401 都会
 * `clearSession` + 跳登录页，报表组件随之卸载，组件内状态一并销毁。
 */
import type { AvailabilityReport } from './api'
import { authToken, currentUser } from './auth'

interface Slot {
  report: AvailabilityReport
  owner: string
}

let slot: Slot | null = null

/**
 * 当前会话指纹。请求发起前取一次、回来时再比对，防止旧会话的结果写进新会话。
 *
 * 未登录时返回空串而不是 `"||"`：三个空值拼出来是非空字符串，调用方的 `!owner`
 * 判断会失效。这里必须显式短路，否则「没登录」会被当成一个合法归属。
 */
export function snapshotOwner(): string {
  if (!authToken.value) return ''
  return `${authToken.value}|${currentUser.value?.id ?? ''}|${currentUser.value?.role ?? ''}`
}

/** 只接受默认窗口（无场站/设备筛选）的可用率报表；owner 由调用方在发起请求时取 */
export function putSnapshot(report: AvailabilityReport, owner: string): void {
  /* 请求期间登出或换了人：这份结果已经不属于当前会话，宁可不写 */
  if (!owner || owner !== snapshotOwner()) return
  slot = { report, owner }
}

/** 取走即清空。归属不符就地丢弃，调用方按「没有快照」走正常查询。 */
export function takeSnapshot(): AvailabilityReport | null {
  const item = slot
  slot = null
  if (!item || !item.owner || item.owner !== snapshotOwner()) return null
  return item.report
}
