import { describe, it, expect } from 'vitest'
import { attentionOrder, reasonLabels } from './interpretations'
import type { Interpretation } from '../types'

const item = (id: number, level = 'major', active = true, read = false) => ({ id, level, active, read, occurred_at: `2026-10-01T00:00:0${id}Z` }) as Interpretation
describe('interpretation attention', () => {
  it('prioritizes urgent events then recent events', () => {
    expect(attentionOrder([item(1), item(2, 'urgent'), item(3)]).map(i => i.id)).toEqual([2, 3, 1])
  })
  it('keeps recovered and read events in the attention list', () => {
    expect(attentionOrder([item(1, 'major', false), item(2, 'major', true, true)]).map(i => i.id)).toEqual([2, 1])
  })
  it('labels truncated and rejected model answers', () => {
    expect(reasonLabels.answer_truncated).toContain('截断')
    expect(reasonLabels.answer_validation_failed).toContain('证据校验')
  })
})
