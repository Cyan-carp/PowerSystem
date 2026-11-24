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
 * 它只保护「交接」这一段。跨页面留存由 report-state 负责，两者共用
 * `sessionFingerprint()` 判归属：登出、换人、降权，两份数据同时读不到。
 */
import type { AvailabilityReport } from './api'
import { sessionFingerprint } from './auth'

interface Slot {
  report: AvailabilityReport
  owner: string
}

let slot: Slot | null = null

/**
 * 只接受默认窗口（无场站/设备筛选）的可用率报表；owner 由调用方在发起请求时取。
 *
 * owner 取的是 `sessionFingerprint()`，报表留存（report-state）用的是同一份指纹。
 */
export function putSnapshot(report: AvailabilityReport, owner: string): void {
  /* 请求期间登出或换了人：这份结果已经不属于当前会话，宁可不写 */
  if (!owner || owner !== sessionFingerprint()) return
  slot = { report, owner }
}

/** 取走即清空。归属不符就地丢弃，调用方按「没有快照」走正常查询。 */
export function takeSnapshot(): AvailabilityReport | null {
  const item = slot
  slot = null
  if (!item || !item.owner || item.owner !== sessionFingerprint()) return null
  return item.report
}
