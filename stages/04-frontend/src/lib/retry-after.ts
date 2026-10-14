/** Retry-After supports delay seconds and an HTTP date, independently of body format. */
export function retryAfterSeconds(value: string | null | undefined, now = Date.now()): number {
  const text = value?.trim()
  if (!text) return 0
  if (/^\d+$/.test(text)) { const seconds = Number(text); return Number.isFinite(seconds) ? seconds : 0 }
  const deadline = Date.parse(text)
  return Number.isFinite(deadline) ? Math.max(0, (deadline - now) / 1000) : 0
}
