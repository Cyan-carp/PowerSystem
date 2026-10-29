/**
 * 自研轻提示与确认框：替代 Element Plus 的 ElMessage / ElMessageBox。
 *
 * 用法：
 *   toast('ok', '已删除 3 条任务记录')
 *   if (await askConfirm({ title: '批量删除任务记录', body: '…', confirmText: '删除', danger: true })) { … }
 *
 * 宿主组件 <ToastHost /> 挂在 App.vue 根部，生命周期由它负责。
 */
import { reactive } from 'vue'

export type ToastKind = 'ok' | 'warn' | 'bad' | 'info'

export interface ToastItem {
  id: number
  kind: ToastKind
  text: string
}

export const toasts = reactive<ToastItem[]>([])

let seq = 0

export function dismissToast(id: number): void {
  const index = toasts.findIndex((item) => item.id === id)
  if (index >= 0) toasts.splice(index, 1)
}

export function toast(kind: ToastKind, text: string, ttl = 3600): void {
  const id = ++seq
  toasts.push({ id, kind, text })
  window.setTimeout(() => dismissToast(id), ttl)
}

export interface ConfirmOptions {
  title: string
  body: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

export interface ConfirmState extends ConfirmOptions {
  open: boolean
  resolve?: (value: boolean) => void
}

export const confirmState = reactive<ConfirmState>({
  open: false,
  title: '',
  body: '',
  confirmText: '确认',
  cancelText: '取消',
  danger: false,
})

/** 关掉确认框并回填结果；调用方 await 这个 Promise 即可。 */
export function settleConfirm(value: boolean): void {
  const resolve = confirmState.resolve
  confirmState.open = false
  confirmState.resolve = undefined
  resolve?.(value)
}

export function askConfirm(options: ConfirmOptions): Promise<boolean> {
  // 同一时刻只留一个确认框：旧的按「取消」处理，避免 Promise 悬空
  if (confirmState.open) settleConfirm(false)
  return new Promise<boolean>((resolve) => {
    Object.assign(confirmState, {
      open: true,
      title: options.title,
      body: options.body,
      confirmText: options.confirmText || '确认',
      cancelText: options.cancelText || '取消',
      danger: options.danger === true,
      resolve,
    })
  })
}
