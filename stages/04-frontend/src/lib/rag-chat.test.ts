import { describe, expect, it } from 'vitest'
import { sourceLink, programReplyLabel, canVerify } from './rag-chat'

describe('program replies and verification availability', () => {
  it('distinguishes program replies from model failures and hides verification', () => {
    expect(programReplyLabel({ response_mode: 'preset' })).toBe('固定回复 · 未调用模型')
    expect(programReplyLabel({ response_mode: 'boundary' })).toBe('服务范围提示 · 未调用模型')
    expect(canVerify({ response_mode: 'boundary', verification_available: true })).toBe(false)
    expect(canVerify({ response_mode: 'preset' })).toBe(false)
  })
  it('retains old response compatibility and respects feature configuration', () => {
    expect(programReplyLabel({})).toBe('')
    expect(canVerify({})).toBe(true)
    expect(canVerify({ response_mode: 'model', verification_available: false })).toBe(false)
    expect(canVerify({ response_mode: 'model', verification_available: true })).toBe(true)
  })
})

describe('RAG source links', () => {
  it('preserves ordinary search query parameters', () => {
    expect(sourceLink('https://example.com/read?id=123')).toBe('https://example.com/read?id=123')
  })
  it('does not render executable or credential-bearing links', () => {
    expect(sourceLink('javascript:alert(1)')).toBeNull()
    expect(sourceLink('https://user:secret@example.com')).toBeNull()
  })
})
