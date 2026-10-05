import { describe, expect, it } from 'vitest'
import { claimEvidence, evidenceRoute, publicLink, requiresEquipmentReview } from './agent-chat'
import { guides } from './page-help'
import type { ChatEvidence } from '../types'

describe('chat evidence boundaries', () => {
  it('warns on physical and uncertain advice, including historical records', () => {
    expect(requiresEquipmentReview({ text: '断电复位', evidence_ids: ['E1'] })).toBe(true)
    expect(requiresEquipmentReview({ text: '检查设备', evidence_ids: ['E1'] })).toBe(true)
    expect(requiresEquipmentReview({ text: '查看日志并检查设备冷却情况', evidence_ids: ['E1'] })).toBe(true)
    expect(requiresEquipmentReview({ text: '查看日志并打开配电柜', evidence_ids: ['E1'] })).toBe(true)
    expect(requiresEquipmentReview({ text: '查阅资料之后进入运行模式', evidence_ids: ['E1'] })).toBe(true)
    expect(requiresEquipmentReview({ text: '查阅厂家手册', evidence_ids: ['E1'] })).toBe(false)
    expect(requiresEquipmentReview({ text: '人工查看', evidence_ids: ['E1'], equipment_operation: true })).toBe(true)
  })
  it('expands only this suggestion citations and keeps historical evidence as event data', () => {
    const items = [{ id: 'E1', source: '/api/v1/alarms/1', data: {} }, { id: 'E2', source: 'https://docs.example.org', kind: 'web', data: {} }] as ChatEvidence[]
    const result = claimEvidence({ text: '建议', evidence_ids: ['E1'] }, items)
    expect(result.map(item => item.id)).toEqual(['E1'])
    expect(result[0]?.kind).toBe('business')
    expect(claimEvidence({ text: '建议', evidence_ids: ['E2'] }, items)[0]?.kind).toBe('web')
    expect(claimEvidence({ text: '建议', evidence_ids: ['missing'] }, items)).toEqual([])
  })
  it('rejects executable, credential-bearing and internal links', () => {
    for (const url of ['javascript:alert(1)', 'http://example.org', 'https://user:pass@example.org', 'https://127.0.0.1/', 'https://host.local/', 'https://example.org/?token=x', 'https://example.org:8092/']) expect(publicLink(url)).toBeNull()
    expect(publicLink('https://docs.example.org/guide')).toBe('https://docs.example.org/guide')
  })
  it('only builds business navigation from known API paths', () => {
    const evidence = { kind: 'business', source: '/api/v1/devices/12/telemetry/latest' } as ChatEvidence
    expect(evidenceRoute(evidence)).toBe('/devices/12')
    expect(evidenceRoute({ ...evidence, source: 'https://evil.org/devices/12' })).toBeNull()
    expect(evidenceRoute({ ...evidence, kind: 'web' })).toBeNull()
  })
  it('covers each visible business route with offline instructions', () => {
    for (const name of ['dashboard', 'devices', 'device-detail', 'alarms', 'predictions', 'agent-chat']) {
      expect(guides[name]?.steps.length).toBeGreaterThan(0)
      expect(guides[name]?.question).toBeTruthy()
    }
  })
})
