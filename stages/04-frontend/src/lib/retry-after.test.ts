import { describe, expect, it } from 'vitest'
import { retryAfterSeconds } from './retry-after'
describe('HTTP Retry-After', () => {
  it('handles numeric delays without a response body dependency', () => {
    expect(retryAfterSeconds('8')).toBe(8)
    expect(retryAfterSeconds(' 0 ')).toBe(0)
    for (const value of [undefined, null, '', 'invalid', 'Infinity']) expect(retryAfterSeconds(value)).toBe(0)
  })
  it('honours a future HTTP date and accepts an expired date as zero delay', () => {
    const now = Date.parse('Mon, 12 Oct 2026 00:00:00 GMT')
    expect(retryAfterSeconds('Mon, 12 Oct 2026 00:00:08 GMT', now)).toBe(8)
    expect(retryAfterSeconds('Sun, 11 Oct 2026 23:59:59 GMT', now)).toBe(0)
  })
})
