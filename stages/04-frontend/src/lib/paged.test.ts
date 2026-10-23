import { describe, expect, it } from 'vitest'
import { nextTick, ref } from 'vue'
import { usePaged } from './paged'

describe('client-side pagination', () => {
  it('renders only the first page by default', () => {
    const source = ref(Array.from({ length: 45 }, (_, i) => i + 1))
    const paged = usePaged(() => source.value, 20)
    expect(paged.rows.value).toHaveLength(20)
    expect(paged.rows.value[0]).toBe(1)
    expect(paged.total.value).toBe(45)
    expect(paged.pageCount.value).toBe(3)
  })

  it('slices later pages and leaves the final page partial', () => {
    const source = ref(Array.from({ length: 45 }, (_, i) => i + 1))
    const paged = usePaged(() => source.value, 20)
    paged.page.value = 2
    expect(paged.rows.value).toEqual([21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40])
    paged.page.value = 3
    expect(paged.rows.value).toHaveLength(5)
  })

  it('falls back to the last populated page when a refresh shrinks the result set', async () => {
    const source = ref(Array.from({ length: 45 }, (_, i) => i + 1))
    const paged = usePaged(() => source.value, 20)
    paged.page.value = 3
    source.value = source.value.slice(0, 25)
    await nextTick()
    expect(paged.pageCount.value).toBe(2)
    expect(paged.page.value).toBe(2)
    expect(paged.rows.value).toHaveLength(5)
  })

  it('returns to the first page on reset and tolerates an empty result set', async () => {
    const source = ref<number[]>([])
    const paged = usePaged(() => source.value, 20)
    expect(paged.rows.value).toEqual([])
    expect(paged.pageCount.value).toBe(1)
    source.value = Array.from({ length: 30 }, (_, i) => i + 1)
    paged.page.value = 2
    paged.reset()
    expect(paged.rows.value[0]).toBe(1)
    await nextTick()
    expect(paged.page.value).toBe(1)
  })
})
