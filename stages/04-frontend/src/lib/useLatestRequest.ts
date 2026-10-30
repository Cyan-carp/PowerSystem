import { onScopeDispose, ref, shallowRef } from 'vue'

/** 每个区域独立更新；替换请求或离开页面后，旧响应不能覆盖当前结果。 */
export function useLatestRequest<T>() {
  const data = shallowRef<T | null>(null)
  const pending = ref(false)
  const error = ref('')
  let controller: AbortController | undefined

  const cancel = () => {
    controller?.abort()
    controller = undefined
    pending.value = false
    data.value = null
    error.value = ''
  }

  const run = async (load: (signal: AbortSignal) => Promise<T>, options: { keepData?: boolean } = {}) => {
    const previous = data.value
    cancel()
    if (options.keepData) data.value = previous
    const current = new AbortController()
    controller = current
    pending.value = true
    try {
      const result = await load(current.signal)
      if (controller === current) data.value = result
    } catch (reason) {
      if (controller === current) error.value = reason instanceof Error ? reason.message : String(reason)
    } finally {
      if (controller === current) pending.value = false
    }
  }

  onScopeDispose(cancel)
  return { data, pending, error, run, cancel }
}
