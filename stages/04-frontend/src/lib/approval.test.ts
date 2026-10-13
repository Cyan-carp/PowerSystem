import { describe, it, expect } from 'vitest'
import { canDecideApproval } from './ops-task'

describe('program approval visibility', () => {
  const expires = '2026-10-12T05:00:00Z', now = Date.parse('2026-10-12T04:00:00Z')
  it('only current admin pending task within expiry', () => {
    expect(canDecideApproval('admin', 'awaiting_approval', 'pending', expires, now)).toBe(true)
    expect(canDecideApproval('operator', 'awaiting_approval', 'pending', expires, now)).toBe(false)
    expect(canDecideApproval('admin', 'queued', 'pending', expires, now)).toBe(false)
    expect(canDecideApproval('admin', 'awaiting_approval', 'approved', expires, now)).toBe(false)
  })
  it('expired and invalid dates cannot approve', () => {
    expect(canDecideApproval('admin', 'awaiting_approval', 'pending', expires, Date.parse(expires))).toBe(false)
    expect(canDecideApproval('admin', 'awaiting_approval', 'pending', 'invalid', now)).toBe(false)
  })
})
