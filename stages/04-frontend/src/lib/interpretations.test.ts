import { describe, it, expect } from 'vitest'
import { attentionOrder, shouldAutoOpen } from './interpretations'
import type { Interpretation } from '../types'

const item = (id: number, level = 'major', active = true, read = false) => ({ id, level, active, read, occurred_at: `2026-10-01T00:00:0${id}Z` }) as Interpretation
describe('interpretation attention', () => {
  it('prioritizes urgent events then recent events', () => {
    expect(attentionOrder([item(1), item(2, 'urgent'), item(3)]).map(i => i.id)).toEqual([2, 3, 1])
  })
  it('does not reopen read, recovered or already shown events', () => {
    expect(shouldAutoOpen(item(1), new Set())).toBe(true)
    expect(shouldAutoOpen(item(1), new Set([1]))).toBe(false)
    expect(shouldAutoOpen(item(1, 'major', false), new Set())).toBe(false)
    expect(shouldAutoOpen(item(1, 'major', true, true), new Set())).toBe(false)
  })
})
