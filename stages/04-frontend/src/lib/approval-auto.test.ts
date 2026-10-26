import { describe, expect, it, beforeEach } from 'vitest'
import { autoApprovalState, claimAutoApproval, releaseAutoApproval, resetAutoApproval } from './approval-auto'

describe('审批弹窗自动弹出闸门', () => {
  beforeEach(() => resetAutoApproval())

  it('首次认领通过，同一动作再次认领被拒绝', () => {
    expect(claimAutoApproval('run-1:act-1')).toBe(true)
    expect(claimAutoApproval('run-1:act-1')).toBe(false)
    expect(claimAutoApproval('')).toBe(false)
  })

  it('同一时刻只允许一个弹窗占用，前一个释放后下一个才能认领', () => {
    expect(claimAutoApproval('run-1:act-1')).toBe(true)
    expect(claimAutoApproval('run-2:act-9')).toBe(false)
    releaseAutoApproval('run-1:act-1')
    expect(claimAutoApproval('run-2:act-9')).toBe(true)
  })

  it('释放不匹配的键不会误放行正在持有的弹窗', () => {
    expect(claimAutoApproval('run-1:act-1')).toBe(true)
    releaseAutoApproval('run-9:act-9')
    expect(autoApprovalState().owner).toBe('run-1:act-1')
    expect(claimAutoApproval('run-2:act-9')).toBe(false)
  })

  it('已弹过的动作在释放后也不再自动弹', () => {
    expect(claimAutoApproval('run-1:act-1')).toBe(true)
    releaseAutoApproval('run-1:act-1')
    expect(claimAutoApproval('run-1:act-1')).toBe(false)
    expect(autoApprovalState().shown).toEqual(['run-1:act-1'])
  })
})
