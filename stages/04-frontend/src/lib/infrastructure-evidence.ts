import type { InfrastructureEvidence } from '../types'

const names: Record<string, string> = {
  'assets.list': '项目资产', 'linux.get_cpu_usage': '主机 CPU', 'linux.get_memory_usage': '主机内存',
  'linux.get_disk_status': '文件系统容量', 'linux.list_services': '项目服务', 'linux.get_system_logs': '服务事件摘要',
  'docker.list_containers': '登记容器', 'docker.inspect_container': '容器状态',
  'docker.get_container_stats': '容器资源', 'docker.get_container_logs': '容器事件摘要',
  'postgresql.get_db_status': '数据库状态', 'postgresql.get_connection_stats': '数据库连接统计',
}
export const infrastructureToolLabel = (tool: string): string => names[tool] || tool
export const vantageLabel = (vantage?: string | null): string => ({ host: '主机采集', container: '容器采集', database: '数据库采集', registry: '资产台账' })[vantage || ''] || '采集未完成'
export function infrastructureNotice(item: InfrastructureEvidence, historical = false): string {
  const notes = historical ? ['历史取证，不代表当前状态'] : []
  if (item.status === 'error') notes.push('读取失败，不能据此判断状态')
  if (item.filter_policy || (['linux.get_system_logs', 'docker.get_container_logs'].includes(item.tool) && item.complete === false)) notes.push('过滤或截断的摘要，不代表完整日志')
  if (item.truncated) notes.push('已截断')
  if (item.filtered_lines !== undefined) notes.push(`已过滤 ${item.filtered_lines} 行`)
  return notes.join(' · ')
}
