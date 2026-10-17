import type { AgentEvidence, Interpretation, Metric } from '../types'
import { normalizePoints, metricLabels, metricUnits } from './format'
import { fixedNumber, predictionFeatures } from './detailPresentation'
import { readableInterpretationText } from './interpretation-display'

const record = (value: unknown): Record<string, unknown> => value != null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null

/** Display only: preserve identifiers, URLs, timestamps and the immutable original text. */
export function interpretationText(text: string | undefined): string {
  return readableInterpretationText(text).split(/((?:https?:\/\/|\/api\/)[^\s，。；、）)]+|[A-Za-z_][\w.-]*=[^\s，。；、）)]+)/g)
    .map((part, index) => index % 2 ? part : part
      .replace(/\b[a-z]+(?:_[a-z]+)+\b/g, word => predictionFeatures[word]?.label ?? word)
      .replace(/(?<![\w./:#=])[-+]?\d+\.\d+(?![\w./])/g, value => fixedNumber(Number(value))))
    .join('')
}

export function interpretationParagraphs(text: string | undefined): string[] {
  return interpretationText(text).split(/(?<=[。！？；])\s*|\n+/).map(line => line.trim()).filter(Boolean)
}

export function interpretationLayout(item: Interpretation) {
  const evidence = item.evidence ?? []
  const find = (tool: string) => evidence.find(e => e.tool === tool && e.status === 'ok')
  const device = record(find('device')?.data ?? find('get_device')?.data)
  const prediction = find('get_prediction')
  const telemetry = find('get_telemetry')
  const history = find('list_alarms')
  const data = record(prediction?.data)
  const metric = typeof telemetry?.data.metric === 'string' && telemetry.data.metric in metricLabels ? telemetry.data.metric as Metric : null
  const statistics = record(telemetry?.data.statistics)
  const rawFactors = Array.isArray(data.top_factors) ? data.top_factors : []
  const factors = rawFactors.map(record).filter(f => typeof f.feature === 'string').map(f => ({
    key: f.feature as string, label: predictionFeatures[f.feature as string]?.label ?? f.feature as string,
    description: predictionFeatures[f.feature as string]?.description ?? '原始模型特征，尚未配置中文说明。',
    unit: predictionFeatures[f.feature as string]?.unit ?? '', value: number(f.value), contribution: number(f.contribution),
  }))
  const historyItems = Array.isArray(history?.data.list) ? history.data.list.map(record).filter(row => row.metric === item.alarm?.metric) : []
  const historyValues = historyItems.flatMap(row => number(row.value) == null ? [] : [number(row.value)!])
  return {
    device: typeof device.device_code === 'string' ? device.device_code : item.alarm ? `设备 #${item.alarm.device_id}` : '平台监控',
    paragraphs: interpretationParagraphs(item.result?.conclusion?.text),
    prediction, factors, modelVersion: typeof data.model_version === 'string' ? data.model_version : '',
    telemetry, metric, unit: metric ? metricUnits[metric] : '',
    points: telemetryPoints(telemetry),
    statistics: [
      { label: '最低值', value: number(statistics.min) }, { label: '平均值', value: number(statistics.mean) }, { label: '最高值', value: number(statistics.max) },
    ],
    sampleCount: number(statistics.count),
    history: history && historyItems.length ? { id: history.id, count: historyItems.length, complete: history.data.complete === true,
      min: historyValues.length ? Math.min(...historyValues) : null, max: historyValues.length ? Math.max(...historyValues) : null } : null,
  }
}

function telemetryPoints(evidence: AgentEvidence | undefined): [number, number][] {
  if (!evidence || !Array.isArray(evidence.data.points)) return []
  const points = evidence.data.points.filter((p): p is [number | string, number] => Array.isArray(p) && p.length === 2
    && (typeof p[0] === 'number' || typeof p[0] === 'string') && typeof p[1] === 'number' && Number.isFinite(p[1]))
  return normalizePoints(points)
}
