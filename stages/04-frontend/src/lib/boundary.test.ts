import { describe, expect, it } from 'vitest'
import { dataTime, evidenceLabel, freshnessLabel } from './boundary'

describe('business evidence presentation', () => {
  it('formats tool time in Beijing without changing raw values', () => {
    expect(dataTime('2026-10-11T03:01:22.037589Z')).toContain('11:01:22')
    expect(dataTime('2026-10-11T03:01:22.037589Z')).toContain('2026.10.11')
    expect(dataTime('2026-10-11T03:01:22.037589Z')).toContain('北京时间')
    expect(dataTime(undefined)).toBe('缺失或无法解析')
    expect(dataTime('invalid')).toBe('缺失或无法解析')
  })
  it('does not equate query success with verified prose or normal equipment', () => {
    expect(evidenceLabel('not_queried')).toBe('本轮未查询平台数据')
    expect(evidenceLabel('queried')).toBe('平台查询成功')
    expect(evidenceLabel('partial_failed')).toContain('部分失败')
    expect(freshnessLabel('stale')).toBe('数据已过期')
    expect(freshnessLabel('future')).toContain('时间异常')
  })
})
