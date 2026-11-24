/**
 * report-snapshot 的交接语义：取走即清空，归属不符即作废。
 * 这里锁的是「谁能读到」和「能读几次」，不涉及报表内容本身。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import type { AvailabilityReport } from './api'
import { authToken, currentUser } from './auth'
import { putSnapshot, snapshotOwner, takeSnapshot } from './report-snapshot'

const report = (computedAt: string) =>
  ({
    rule_version: 'availability-v1',
    computed_at: computedAt,
    start: '2026-11-17T01:30:00Z',
    end: '2026-11-24T01:30:00Z',
    devices: [],
    stations: [],
  }) as AvailabilityReport

const signIn = (id: number, role = 'operator', token = `token-${id}`) => {
  authToken.value = token
  currentUser.value = { id, username: `u${id}`, real_name: '', role }
}

const signOut = () => {
  authToken.value = ''
  currentUser.value = null
}

beforeEach(() => {
  /* 模块级单槽在用例之间会串，先取空一次 */
  signIn(1)
  takeSnapshot()
  signOut()
})

describe('交接', () => {
  it('同会话写入后能取到同一份对象，且只能取一次', () => {
    signIn(7)
    const value = report('2026-11-24T01:30:03Z')
    putSnapshot(value, snapshotOwner())

    expect(takeSnapshot()).toBe(value)
    expect(takeSnapshot()).toBeNull()
  })

  it('没有登录态时写入被忽略', () => {
    signOut()
    putSnapshot(report('2026-11-24T01:30:03Z'), 'whatever')

    signIn(7)
    expect(takeSnapshot()).toBeNull()
  })
})

describe('会话指纹', () => {
  it('未登录时指纹为空串，不能是 "||" 这种非空占位', () => {
    signOut()
    /* 三个空值拼出来是 "||"，truthy，会让调用方的 `!owner` 守卫失效 */
    expect(snapshotOwner()).toBe('')

    signIn(7)
    expect(snapshotOwner()).not.toBe('')
  })

  it('未登录时即使拿着空指纹也写不进去', () => {
    signOut()
    const anonymousOwner = snapshotOwner()
    putSnapshot(report('2026-11-24T01:30:03Z'), anonymousOwner)

    /* 重新登录后读不到，说明那份数据压根没落进单槽 */
    signIn(7)
    expect(takeSnapshot()).toBeNull()
  })
})

describe('归属', () => {
  it('写入后退登出，旧结果取不出来', () => {
    signIn(7)
    putSnapshot(report('2026-11-24T01:30:03Z'), snapshotOwner())
    signOut()

    expect(takeSnapshot()).toBeNull()
  })

  it('换了角色就不再复用（同一账号降权后不能看旧结果）', () => {
    signIn(7, 'admin')
    putSnapshot(report('2026-11-24T01:30:03Z'), snapshotOwner())

    signIn(7, 'viewer')
    expect(takeSnapshot()).toBeNull()
  })

  it('换 token 视为换会话，即使账号 id 相同也不复用', () => {
    signIn(7, 'operator', 'token-a')
    putSnapshot(report('2026-11-24T01:30:03Z'), snapshotOwner())

    signIn(7, 'operator', 'token-b')
    expect(takeSnapshot()).toBeNull()
  })

  it('请求期间登出：发起时的 owner 与写入时不一致，不写入', () => {
    signIn(7)
    const ownerWhenRequested = snapshotOwner()
    signOut()

    putSnapshot(report('2026-11-24T01:30:03Z'), ownerWhenRequested)
    signIn(7)
    expect(takeSnapshot()).toBeNull()
  })

  it('登出再登录（token 不同）：登出前发出的请求迟到也不会被接受', () => {
    signIn(7, 'operator', 'token-old')
    const ownerWhenRequested = snapshotOwner()

    /* 用户登出，随后同一账号重新登录，拿到新 token */
    signOut()
    signIn(7, 'operator', 'token-new')

    /* 旧请求此时才回来 */
    putSnapshot(report('2026-11-24T01:30:03Z'), ownerWhenRequested)
    expect(takeSnapshot()).toBeNull()
  })

  it('降权期间没人消费、随后恢复原角色：仍可复用（同一份查询，数据未变）', () => {
    signIn(7, 'admin')
    const value = report('2026-11-24T01:30:03Z')
    putSnapshot(value, snapshotOwner())

    /* 中间角色变化过，但没有任何一次 take 把槽清空 */
    signIn(7, 'viewer')
    signIn(7, 'admin')

    expect(takeSnapshot()).toBe(value)
  })
})
