/** 制图标注用的格式化：统一小数位与单位，避免同一数字在不同位置长得不一样。 */

export const n = (value: number | null | undefined, digits = 0): string => {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export const kw = (value: number | null | undefined): string => {
  if (value == null) return '—'
  if (Math.abs(value) >= 1000) return `${n(value / 1000, 2)} MW`
  return `${n(value, 1)} kW`
}

export const mwh = (value: number | null | undefined): string => {
  if (value == null) return '—'
  if (Math.abs(value) >= 10000) return `${n(value / 1000, 1)} MWh`
  return `${n(value, 0)} kWh`
}

export const pct = (value: number | null | undefined, digits = 1): string =>
  value == null ? '—' : `${n(value, digits)}%`

/** 秒 → 人类可读的时长；先整体取整，避免出现「56 分 60 秒」。 */
export const span = (seconds: number | null | undefined): string => {
  if (seconds == null) return '—'
  const total = Math.round(seconds)
  if (total < 60) return `${total} 秒`
  if (total < 3600) {
    const minutes = Math.floor(total / 60)
    const rest = total % 60
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
  }
  const totalMinutes = Math.round(total / 60)
  const hours = Math.floor(totalMinutes / 60)
  const restMinutes = totalMinutes % 60
  return restMinutes ? `${hours} 时 ${restMinutes} 分` : `${hours} 小时`
}

export const bytes = (value: number): string => {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${n(value / 1024, 1)} KB`
  return `${n(value / 1024 / 1024, 1)} MB`
}

export const clock = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export const stamp = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

/** 时间戳 → 北京时间 HH:MM，用于曲线横轴与预测窗口；缺失值显示「—」。 */
export const timeOf = (ts: number | null | undefined): string =>
  ts == null
    ? '—'
    : new Date(ts).toLocaleTimeString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
      })

/** 时间戳 → 北京时间 HH:MM:SS，用于「采样时间」这类需要精确到秒的列。 */
export const clockMs = (ts: number | null | undefined): string =>
  ts == null
    ? '—'
    : new Date(ts).toLocaleTimeString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })

export const across = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  // 将来的时刻（下次执行、预测窗口）不能落到「刚刚」：符号反了就换成「后」
  if (diff < -10) return `${span(-diff)}后`
  if (diff < 10) return '刚刚'
  if (diff < 60) return `${Math.round(diff)} 秒前`
  return `${span(diff)}前`
}

export const METRIC_CN: Record<string, string> = {
  temperature: '温度',
  voltage: '电压',
  current: '电流',
  power: '功率',
  ai_failure_risk: '失效风险',
  simulation_scenario: '演示场景',
}

/** 遥测状态码：0 停机 / 1 运行 / 2 故障（与边侧上报约定一致）。 */
export const TELEMETRY_STATE_CN: Record<number, string> = {
  0: '停机',
  1: '运行',
  2: '故障',
}

/** 各指标读数单位；失效风险是 0–1 概率，无单位。 */
export const METRIC_UNIT: Record<string, string> = {
  temperature: '℃',
  voltage: 'V',
  current: 'A',
  power: 'kW',
  ai_failure_risk: '',
}

/** 读数小数位：概率需要两位，其余一位即可。 */
export const METRIC_DIGITS: Record<string, number> = {
  temperature: 1,
  voltage: 1,
  current: 1,
  power: 1,
  ai_failure_risk: 2,
}

export const LEVEL_CN: Record<string, string> = {
  urgent: '紧急',
  major: '重要',
  minor: '次要',
}

export const ALARM_STATE_CN: Record<string, string> = {
  unhandled: '未处理',
  acked: '已确认',
  recovered: '已恢复',
}

/** 健康度状态：真后端 health.go 只返回 online / offline / fault / unknown 四种 */
export const HEALTH_STATE_CN: Record<string, string> = {
  online: '在线',
  offline: '离线',
  fault: '故障',
  unknown: '未知',
}

/* 健康分依据：health.go 的 matched_by 是固定枚举，中文说明是从评分分支翻译过来的。
   列表用短句（列宽有限），抽屉里给完整解释（值班员要靠它判断「0 分是不是等于坏了」）。 */
const HEALTH_BASIS: Record<string, [short: string, full: string]> = {
  'no valid sample': ['无合法样本', '从未取得合法采样，分数未知，不参与排名'],
  'latest cache unavailable': ['缓存读取失败', '最新数据读取失败，分数未知'],
  'latest payload invalid': ['数据格式异常', '最新数据格式异常，分数未知'],
  'latest payload incomplete or invalid': ['字段不全', '最新数据缺少必要字段或状态值非法，分数未知'],
  'reception time anomaly': ['接收时间异常', '接收时间异常，分数未知'],
  'sample time anomaly': ['采样时间异常', '采样时间异常，分数未知'],
  'stale reception (>15s)': ['接收超 15 秒', '接收超过 15 秒，按连接离线计 0 分；不代表已证实硬件故障'],
  'stale sample (>15s)': ['采样已过期', '接收新鲜但采样已过期，保留未知、不按 0 分处理'],
  'status=2 fault': ['遥测 status=2', '最新遥测上报 status=2（故障），计 0 分'],
  'highest active alarm level': ['最高活动告警', '在线非故障，按最高活动告警级别取值：紧急 40 / 重要 70 / 一般 90'],
  'online with no active alarm': ['在线无告警', '在线、非故障且查不到活动告警，记 100 分'],
}

export const healthBasisShort = (basis: string | null | undefined): string =>
  (basis && HEALTH_BASIS[basis]?.[0]) || basis || '—'

export const healthBasis = (basis: string | null | undefined): string =>
  (basis && HEALTH_BASIS[basis]?.[1]) || basis || '—'

export const EDGE_STATE_CN: Record<string, string> = {
  online: '在线',
  degraded: '受限',
  offline: '断链',
}

export const SYNC_STATE_CN: Record<string, string> = {
  synced: '已同步',
  lagging: '滞后',
  blocked: '阻塞',
}

export const KIND_CN: Record<string, string> = {
  telemetry: '遥测',
  alarm: '告警',
  audit: '审计',
}

/** 状态 → 语义色变量名，组件里只引变量不写死色值。 */
export const toneOf = (state: string): 'ok' | 'warn' | 'bad' | 'idle' => {
  if (state === 'online' || state === 'synced' || state === 'recovered') return 'ok'
  if (state === 'degraded' || state === 'lagging' || state === 'major' || state === 'minor') return 'warn'
  if (state === 'offline' || state === 'blocked' || state === 'urgent' || state === 'unhandled' || state === 'fault') return 'bad'
  return 'idle'
}
