import { onScopeDispose, ref, shallowRef } from 'vue'
import type { Ref } from 'vue'
import { api } from './api'
import type { Device } from './api'

/** A complete device selector, with one shared in-flight load per page. */
export function useDeviceChoices(selected: Ref<number>) {
  const devices = shallowRef<Device[]>([])
  const loading = ref(false)
  const error = ref('')
  let loaded = false
  let inflight: Promise<void> | null = null
  const lifetime = new AbortController()
  onScopeDispose(() => lifetime.abort())

  const load = (): Promise<void> => {
    if (inflight) return inflight
    if (loaded || lifetime.signal.aborted) return Promise.resolve()
    loading.value = true
    error.value = ''
    inflight = (async () => {
      try {
        const rows = new Map<number, Device>()
        for (let page = 1; ; page++) {
          lifetime.signal.throwIfAborted()
          const result = await api.devices({ scope: 'all', page, pageSize: 100 })
          lifetime.signal.throwIfAborted()
          const previous = rows.size
          for (const device of result.list) rows.set(device.id, device)
          if (rows.size >= result.total) break
          if (rows.size === previous) throw new Error('设备清单读取不完整，请重试')
        }
        devices.value = [...rows.values()]
        loaded = true
        if (!devices.value.some(device => device.id === selected.value)) {
          selected.value = devices.value[0]?.id ?? 0
        }
      } catch (reason) {
        if (!lifetime.signal.aborted) error.value = reason instanceof Error ? reason.message : String(reason)
      } finally {
        loading.value = false
        inflight = null
      }
    })()
    return inflight
  }
  return { devices, loading, error, load }
}
