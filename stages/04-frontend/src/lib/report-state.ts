/**
 * 报表页的会话内留存：切去别的页面核对一下再回来，看到的必须还是同一份结果。
 *
 * 为什么需要它：报表页每次挂载如果没命中首页快照，就会按「此刻往前 7 天」重算一遍。
 * 窗口往后挪了几分钟，数字就全变了，用户刚看到的那一行再也找不回来。
 * 业务上要的是「只有我按刷新，数字才变」，所以把整份页面状态留在这里。
 *
 * 规则只有一条：**只有「查询 / 刷新」能改写这份状态**，其余路径（页签切换、
 * 来回导航、浏览器后退）一律读它。因此这里没有过期时间、没有 LRU、没有后台预热，
 * 也不做在飞复用——要不要新数据由用户按按钮决定，界面用「生成于 X」把时间说清楚。
 *
 * 与快照的分工：快照是「首页 → 报表」的一次性交接，取走即清空；
 * 这里是同一份页面在会话内的留存，可反复读，直到下一次查询。
 *
 * 两条安全边界：
 * - 按 `sessionFingerprint()` 判归属，登出 / 换人 / 降权后读不到旧数据；
 * - 不落 localStorage / sessionStorage，F5 与关标签页都会让它消失，
 *   不在磁盘上留上一个账号的报表。
 */
import type { AvailabilityReport, ReportParams, TimelinessReport } from './api'
import { sessionFingerprint } from './auth'

export interface ReportViewState {
  /** 这份结果提交时的查询条件（含起止与筛选） */
  params: ReportParams
  /** 输入框里的字面值，回来时原样填回，不能按「此刻往前 7 天」重算 */
  startText: string
  endText: string
  stationFilter: string
  deviceFilter: string
  /** 两个页签各自的结果，可能只有一半（从没切过去过） */
  availability: AvailabilityReport | null
  timeliness: TimelinessReport | null
}

interface Entry {
  owner: string
  state: ReportViewState
}

let entry: Entry | null = null

/** 没有留存、或留存属于另一个会话（登出换人、token 过期重登）都返回 null */
export function readReportState(): ReportViewState | null {
  if (!entry) return null
  if (!entry.owner || entry.owner !== sessionFingerprint()) {
    entry = null
    return null
  }
  return entry.state
}

/** 未登录时不写：没有会话指纹就没有归属，写进去只会被下一次读取丢掉 */
export function writeReportState(state: ReportViewState): void {
  const owner = sessionFingerprint()
  if (!owner) return
  entry = { owner, state }
}

/** 用例之间清场用；业务路径不需要主动清（换成空会话自然读不到） */
export function clearReportState(): void {
  entry = null
}
