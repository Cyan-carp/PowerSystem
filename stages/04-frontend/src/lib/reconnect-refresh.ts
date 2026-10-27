import { onMounted, onUnmounted } from 'vue'
import { realtime } from './realtime'

// Call sites pass only read operations. Never remount a view or resubmit its draft.
export function useReconnectRefresh(read: () => void | Promise<void>): void {
  let unsubscribe: (() => void) | undefined
  onMounted(() => { unsubscribe = realtime.subscribe(event => { if (event.type === 'connected') void Promise.resolve().then(read).catch(() => {}) }) })
  onUnmounted(() => unsubscribe?.())
}
