import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'

export interface PagedView<T> {
  page: Ref<number>
  pageSize: Ref<number>
  total: ComputedRef<number>
  pageCount: ComputedRef<number>
  rows: ComputedRef<T[]>
  reset: () => void
}

/**
 * Client-side pagination for result sets the API returns in full (health
 * ranking, availability report). The source is read through a getter so it can
 * be either a ref or a computed filter chain.
 */
export function usePaged<T>(source: () => T[], defaultSize = 20): PagedView<T> {
  const page = ref(1)
  const pageSize = ref(defaultSize)
  const total = computed(() => source().length)
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
  const rows = computed(() => {
    const start = (page.value - 1) * pageSize.value
    return source().slice(start, start + pageSize.value)
  })
  // A refresh can shrink the result set under the current page, which would
  // otherwise render an empty table instead of the last populated page.
  watch(pageCount, count => { if (page.value > count) page.value = count })
  function reset() { page.value = 1 }
  return { page, pageSize, total, pageCount, rows, reset }
}
