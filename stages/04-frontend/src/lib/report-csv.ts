/**
 * 报表 CSV：用页面上这一份快照直接生成，与后端 report_export.go 逐列对齐。
 *
 * 为什么放前端：按 2026-11-24 业务规范，导出必须等于所见。走后端导出会重新查询，
 * 两次计算之间进来的采样会让文件与页面不一致，而且要多付一次 143 台 × 7 天的计算。
 *
 * 与后端保持一致的细节（改这里必须同步 internal/api/report_export.go）：
 * - 列顺序、列名、单位后缀、空值一律空串；
 * - UTF-8 BOM 开头，行尾 \n，最后一行也带 \n（Go encoding/csv 的行为）；
 * - 含逗号 / 双引号 / CR / LF 的字段加引号，内部引号翻倍；
 * - 以 = + - @ 开头的字段前置单引号，防公式注入（后端 csvCell）；
 * - 浮点用最短往返表示，与 Go strconv.FormatFloat(v,'f',-1,64) 同形。
 *
 * 不做的事：不做增量、不做压缩、不写死中文表头以外的任何业务文案。
 */
import type {
  AvailabilityReport,
  StationAvailabilityRow,
  TimelinessReport,
  TimelinessStat,
} from './api'

const BOM = '\ufeff'

/** 与后端 csvCell 相同：只处理公式注入前缀，引号交给下面的标准规则 */
const protect = (value: string): string =>
  value && '=+-@'.includes(value[0]) ? `'${value}` : value

/** Go csv.Writer 的引号条件：含分隔符、引号、CR/LF，或以空格/Tab 开头 */
const quote = (value: string): string =>
  /[",\r\n]/.test(value) || /^[ \t]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value

const cell = (value: string): string => quote(protect(value))

const line = (cells: string[]): string => cells.map(cell).join(',')

/**
 * Go 的 'f' 格式不用指数记法。比率落在 [0,1]，正常值两边逐字符一致；
 * 低于 1e-6 的极端值（窗口内几乎没有覆盖）走定点兜底，避免 JS 输出 1e-9。
 */
const float = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(value)) return ''
  if (value !== 0 && Math.abs(value) < 1e-6) {
    return value.toFixed(12).replace(/0+$/, '').replace(/\.$/, '')
  }
  return String(value)
}

const int = (value: number | null | undefined): string =>
  value == null || !Number.isFinite(value) ? '' : String(Math.trunc(value))

const bool = (value: boolean): string => (value ? 'true' : 'false')

const AVAILABILITY_HEADER = [
  '范围类型', '设备编码', '场站编码', '有效窗口(ms)', '运行(ms)', '停机(ms)', '故障(ms)',
  '未知(ms)', '覆盖(ms)', '观测可用率(0-1)', '数据覆盖率(0-1)', '数据不足', '规则版本',
  '开始', '结束', '计算时间', '归组依据',
]

const TIMELINESS_HEADER = [
  '范围', '指标（平台记录时间）', '有效样本数', '平均(ms)', '中位数(ms)', 'P90(ms)',
  '未确认', '未恢复', '管理关闭', '历史或原因未知', '事件总数', '异常生命周期',
  '规则版本', '开始', '结束', '计算时间',
]

/** 设备行与场站行共用尾部：规则版本 / 窗口 / 计算时间 / 归组依据 */
const availabilityTail = (report: AvailabilityReport): string[] => [
  report.rule_version,
  report.start,
  report.end,
  report.computed_at,
  '当前台账（非历史归属）',
]

const deviceRow = (report: AvailabilityReport, row: AvailabilityReport['devices'][number]): string[] => [
  'device',
  row.device_code,
  row.station_code,
  int(row.window_ms),
  int(row.running_ms),
  int(row.stopped_ms),
  int(row.fault_ms),
  int(row.unknown_ms),
  int(row.covered_ms),
  float(row.observed_availability),
  float(row.data_coverage),
  bool(row.insufficient_data),
  ...availabilityTail(report),
]

const stationRow = (report: AvailabilityReport, row: StationAvailabilityRow): string[] => [
  'station',
  '',
  row.station_code,
  int(row.window_ms),
  int(row.running_ms),
  int(row.stopped_ms),
  int(row.fault_ms),
  int(row.unknown_ms),
  int(row.covered_ms),
  float(row.observed_availability),
  float(row.data_coverage),
  bool(row.insufficient_data),
  ...availabilityTail(report),
]

/** 完整查询结果：全部设备行 + 全部场站行，不受页面搜索/排序/分页影响 */
export function availabilityCsv(report: AvailabilityReport): string {
  const rows = [
    ...report.devices.map((row) => deviceRow(report, row)),
    ...report.stations.map((row) => stationRow(report, row)),
  ]
  return `${BOM}${[AVAILABILITY_HEADER, ...rows].map(line).join('\n')}\n`
}

const timelinessRow = (
  report: TimelinessReport,
  name: string,
  stat: TimelinessStat,
): string[] => [
  report.scope,
  name,
  int(stat.count),
  float(stat.mean_ms),
  float(stat.median_ms),
  float(stat.p90_ms),
  int(report.result.pending_ack),
  int(report.result.pending_recover),
  int(report.result.managed_recover),
  int(report.result.unknown_recover),
  int(report.result.event_count),
  int(report.result.invalid_lifecycle),
  report.rule_version,
  report.start,
  report.end,
  report.computed_at,
]

export function timelinessCsv(report: TimelinessReport): string {
  const result = report.result
  const rows = [
    timelinessRow(report, '告警确认耗时', result.ack_timeliness),
    timelinessRow(report, '平台告警恢复耗时（合计）', result.recover_timeliness),
    timelinessRow(report, '遥测恢复耗时', result.telemetry_recover_timeliness),
    timelinessRow(report, '预测风险下降耗时', result.prediction_recover_timeliness),
  ]
  return `${BOM}${[TIMELINESS_HEADER, ...rows].map(line).join('\n')}\n`
}

/** 文件名与后端 Content-Disposition 一致，便于两边产物互相对照 */
export function downloadCsv(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}
