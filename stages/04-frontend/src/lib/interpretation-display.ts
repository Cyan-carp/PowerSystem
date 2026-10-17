import type { AgentEvidence } from '../types'
import { metricLabels } from './format'

const alertNames: Record<string, string> = {
  PowerSystemAPIDown: '平台接口监控抓取异常',
  PowerSystemDiskHigh: '服务器磁盘使用率过高',
}

const evidenceTools: Record<string, string> = {
  monitor_event: '平台监控事件',
  get_alarm_detail: '告警详情',
  device: '设备信息',
  get_device: '设备信息',
  list_alarms: '近期告警',
  get_telemetry: '设备历史曲线',
  get_prediction: '故障预测',
}

const evidenceStatuses: Record<string, string> = {
  ok: '已获取',
  no_data: '暂无数据',
  backend_unavailable: '数据服务暂不可用',
  error: '获取失败',
}

const proseTerms: Record<string, string> = {
  count: '数量', severity: '告警级别', synthetic: '合成数据', trained: '已训练',
  threshold: '阈值', complete: '数据完整', probability: '故障概率',
  device_id: '设备编号', device_code: '设备编号', risk_level: '风险等级',
  alertname: '告警名称', model_version: '模型版本', source: '来源',
  status: '状态', fingerprint: '事件标识', endsAt: '结束时间', startsAt: '开始时间',
  value: '数值', vendor: '生产厂家', dev_type: '设备类型', station_code: '场站编号',
  level: '级别', labels: '标签', alarm: '告警',
}

export function alarmMetricLabel(metric: string): string {
  if (metric === 'ai_failure_risk') return 'AI 故障风险'
  if (metric in metricLabels) return metricLabels[metric as keyof typeof metricLabels]
  return metric
}

export function monitorAlertLabel(name: string | undefined): string {
  if (!name) return '平台监控告警'
  return alertNames[name] || name
}

export function monitorSeverityLabel(value: string | undefined): string {
  if (!value) return '级别未提供'
  return { critical: '紧急', warning: '警告', info: '提示', firing: '告警中', resolved: '已恢复' }[value] || value
}

export function evidenceToolLabel(tool: string): string { return evidenceTools[tool] || '事件证据' }
export function evidenceStatusLabel(status: string): string { return evidenceStatuses[status] || status }

export function evidenceSourceLabel(evidence: Pick<AgentEvidence, 'tool' | 'source'>): string {
  const source = evidence.source
  if (evidence.tool === 'monitor_event' || source === 'alertmanager/webhook') return '平台监控通知'
  if (evidence.tool === 'get_alarm_detail' || /^\/api\/v1\/alarms\/\d+/.test(source)) return '告警详情记录'
  if (evidence.tool === 'get_telemetry' || source.includes('/telemetry')) return '设备历史监测数据'
  if (evidence.tool === 'device' || evidence.tool === 'get_device' || /^\/api\/v1\/devices\/\d+/.test(source)) return '设备登记信息'
  if (evidence.tool === 'list_alarms' || source.startsWith('alarm_records:')) return '最近 24 小时告警记录'
  if (evidence.tool === 'get_prediction' || source.startsWith('prediction_records:')) return '触发时故障预测记录'
  return '事件取证记录'
}

// Only localize known UI-facing phrases. Raw evidence stays available unchanged below.
export function readableInterpretationText(text: string | undefined): string {
  if (!text) return ''
  return text.split(/((?:https?:\/\/|\/api\/)[^\s，。；、）)]+|[A-Za-z_][\w.-]*=[^\s，。；、）)]+)/g).map((part, index) => index % 2 ? part : part
    .replace(/\b(\d{4})\/(\d{1,2})\/(\d{1,2})\b/g, (_, year: string, month: string, day: string) => `${year}.${Number(month)}.${Number(day)}`)
    .replaceAll('PowerSystem API scrape failed for over one minute', '平台接口监控连续超过一分钟抓取失败')
    .replaceAll('PowerSystemAPIDown', '平台接口监控抓取异常')
    .replaceAll('PowerSystemDiskHigh', '服务器磁盘使用率过高')
    .replaceAll('PowerSystem API', '平台接口')
    .replaceAll('ai_failure_risk', 'AI 故障风险')
    .replaceAll('synthetic-trained', '合成数据训练')
    .replaceAll('top_factors', '主要影响因素')
    .replace(/(?<![\w/=])API\b/g, '接口')
    .replace(/(?<![\w/=])metric\b/g, '指标')
    .replace(/(?<![\w/=])min\b/g, '最小值')
    .replace(/(?<![\w/=])max\b/g, '最大值')
    .replace(/(?<![\w/=])mean\b/g, '平均值')
    .replace(/(?<![\w/=])temperature(?![\w/])/g, '温度')
    .replace(/(?<![\w/=])voltage(?![\w/])/g, '电压')
    .replace(/(?<![\w/=])current(?![\w/])/g, '电流')
    .replace(/(?<![\w/=])power(?![\w/])/g, '功率')
    .replace(/(?<![\w/=])critical\b/g, '紧急')
    .replace(/(?<![\w/=])warning\b/g, '警告')
    .replace(/(?<![\w/=])major\b/g, '重要')
    .replace(/(?<![\w/=])minor\b/g, '一般')
    .replace(/(?<![\w/=])urgent\b/g, '紧急')
    .replace(/(?<![\w/=])high\b/g, '高风险')
    .replace(/(?<![\w/=])low\b/g, '低风险')
    .replace(/(?<![\w/=])firing\b/g, '告警中')
    .replace(/(?<![\w/=])resolved\b/g, '已恢复')
    .replace(/(?<![\w/=])(count|severity|synthetic|trained|threshold|complete|probability|device_id|device_code|risk_level|alertname|model_version|source|status|fingerprint|endsAt|startsAt|value|vendor|dev_type|station_code|level|labels|alarm)(?![\w/])/g, word => proseTerms[word])).join('')
}
