import { ref, shallowRef } from 'vue'

export type ReportParams = { start: string; end: string; station_code?: string; device_id?: number }

// Each report publishes its result as soon as it completes. A failed or
// superseded request must never enable export of an older selection.
export function useReportPanel<T>(fetch: (params: ReportParams, signal: AbortSignal) => Promise<T>) {
  const data = shallowRef<T | null>(null)
  const loaded = shallowRef<ReportParams | null>(null)
  const loading = ref(false), error = ref('')
  let current: AbortController | null = null
  function cancel() {
    current?.abort(); current = null; loading.value = false
  }
  async function load(params: ReportParams) {
    cancel()
    const controller = new AbortController()
    current = controller
    data.value = null; loaded.value = null; error.value = ''; loading.value = true
    const selection = { ...params }
    try {
      const result = await fetch(selection, controller.signal)
      if (current !== controller) return
      data.value = result; loaded.value = selection
    } catch (cause) {
      if (current === controller) error.value = cause instanceof Error ? cause.message : '报表查询失败'
    } finally {
      if (current === controller) { loading.value = false; current = null }
    }
  }
  return { data, loaded, loading, error, load, cancel }
}
