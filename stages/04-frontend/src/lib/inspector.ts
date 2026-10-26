import { computed, ref } from 'vue'

/**
 * 右侧检视栏（Inspector）。
 *
 * 页面指南与告警解读不再各自弹浮层/抽屉，而是共用工作区右侧的同一块栏位：
 * 打开时推挤主内容（而不是盖住它），同一时刻只承载一个面板。这样用户在看
 * 指南或解读时仍能对照左侧的表格与曲线。
 */
export type InspectorPanel = 'help' | 'interpretation'

const active = ref<InspectorPanel | null>(null)

export const activeInspector = computed(() => active.value)

export function isInspectorOpen(panel: InspectorPanel): boolean {
  return active.value === panel
}

export function openInspector(panel: InspectorPanel): void {
  active.value = panel
}

export function closeInspector(): void {
  active.value = null
}

export function toggleInspector(panel: InspectorPanel): void {
  active.value = active.value === panel ? null : panel
}

/** 路由切换时收起，避免带着上一页的解读进入新页面 */
export function resetInspector(): void {
  active.value = null
}
