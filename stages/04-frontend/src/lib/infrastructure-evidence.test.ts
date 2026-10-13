import { describe, expect, it } from 'vitest'
import type { InfrastructureEvidence } from '../types'
import { infrastructureNotice, infrastructureToolLabel, vantageLabel } from './infrastructure-evidence'

const evidence: InfrastructureEvidence = { call_id: 'one', evidence_ref: 'call:one', tool: 'docker.get_container_logs', tool_version: '1.0', status: 'ok', vantage: 'container', collected_at: '2026-10-12T00:00:00Z', truncated: false, complete: false, filtered_lines: 4 }
describe('基础设施证据的边界展示', () => {
  it('区分主机、容器、数据库和未完成采集', () => {
    expect(vantageLabel('host')).toBe('主机采集'); expect(vantageLabel('container')).toBe('容器采集')
    expect(vantageLabel('database')).toBe('数据库采集'); expect(vantageLabel(null)).toBe('采集未完成')
  })
  it('日志为空仍标不完整并显示过滤数量', () => {
    expect(infrastructureNotice(evidence)).toContain('不代表完整日志')
    expect(infrastructureNotice(evidence)).toContain('4 行')
  })
  it('保留历史、失败和截断语义', () => {
    const text = infrastructureNotice({ ...evidence, status: 'error', truncated: true }, true)
    expect(text).toContain('历史取证'); expect(text).toContain('读取失败'); expect(text).toContain('已截断')
    expect(infrastructureNotice({ ...evidence, complete: true, filtered_lines: undefined })).toBe('')
    expect(infrastructureToolLabel('docker.get_container_stats')).toBe('容器资源')
  })
  it('资源读取失败不误显示日志过滤说明', () => {
    const text = infrastructureNotice({ ...evidence, tool: 'linux.get_cpu_usage', status: 'error', filtered_lines: undefined })
    expect(text).toContain('读取失败'); expect(text).not.toContain('完整日志')
  })
})
