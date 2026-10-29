/**
 * 任务取证相关的文案与 markdown 渲染。
 * 迁自 PowerSystem 原前端的 boundary.ts / infrastructure-evidence.ts / rag-chat.ts 的 renderAnswer 子集。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { InfrastructureEvidence, RAGEvidence } from './ops-types'
import { stamp } from './format'

export function renderAnswer(markdown: string): string {
  return DOMPurify.sanitize(marked.parse(markdown, { async: false }) as string)
}

export function evidenceLabel(state: RAGEvidence['state']): string {
  return {
    not_queried: '本轮未查询平台数据',
    queried: '平台查询成功',
    partial_failed: '平台查询部分失败',
    failed: '平台查询失败',
  }[state]
}

export function dataTime(value?: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return '缺失或无法解析'
  return `${stamp(value)}（北京时间）`
}

export function freshnessLabel(value: string): string {
  return (
    {
      fresh: '数据在时效范围内',
      stale: '数据已过期',
      future: '时间异常：采样时间在未来',
      unknown: '新鲜度无法判断',
    }[value] || '新鲜度无法判断'
  )
}

const toolNames: Record<string, string> = {
  'assets.list': '项目资产',
  'linux.get_cpu_usage': '主机 CPU',
  'linux.get_memory_usage': '主机内存',
  'linux.get_disk_status': '文件系统容量',
  'linux.list_services': '项目服务',
  'linux.get_system_logs': '服务事件摘要',
  'docker.list_containers': '登记容器',
  'docker.inspect_container': '容器状态',
  'docker.get_container_stats': '容器资源',
  'docker.get_container_logs': '容器事件摘要',
  'postgresql.get_db_status': '数据库状态',
  'postgresql.get_connection_stats': '数据库连接统计',
}

export const infrastructureToolLabel = (tool: string): string => toolNames[tool] || tool

export const vantageLabel = (vantage?: string | null): string =>
  ({ host: '主机采集', container: '容器采集', database: '数据库采集', registry: '资产台账' })[vantage || ''] ||
  '采集未完成'

export function infrastructureNotice(item: InfrastructureEvidence, historical = false): string {
  const notes = historical ? ['历史取证，不代表当前状态'] : []
  if (item.status === 'error') notes.push('读取失败，不能据此判断状态')
  if (
    item.filter_policy ||
    (['linux.get_system_logs', 'docker.get_container_logs'].includes(item.tool) && item.complete === false)
  ) {
    notes.push('过滤或截断的摘要，不代表完整日志')
  }
  if (item.truncated) notes.push('已截断')
  if (item.filtered_lines !== undefined) notes.push(`已过滤 ${item.filtered_lines} 行`)
  return notes.join(' · ')
}
