import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { RAGChatResponse } from '../types'

export function programReplyLabel(response: Pick<RAGChatResponse, 'response_mode'>): string {
  return response.response_mode === 'preset' ? '固定回复 · 未调用模型' : response.response_mode === 'boundary' ? '服务范围提示 · 未调用模型' : ''
}

export function canVerify(response: Pick<RAGChatResponse, 'response_mode' | 'verification_available'>): boolean {
  return !programReplyLabel(response) && response.verification_available !== false
}

export function renderAnswer(markdown: string): string {
  return DOMPurify.sanitize(marked.parse(markdown, { async: false }) as string)
}

export function sourceLink(value: string): string | null {
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch { return null }
}
