/**
 * 主题：白天（浅色云控制台）/ 夜间（深灰）。
 *
 * 只做一件事——给 <html> 打 data-theme，颜色全部由 tokens.css 的变量承担。
 * 组件不读主题、不写条件样式，因此新增页面不会漏掉夜间配色。
 *
 * 默认白天：现有用户看到的第一眼必须和以前一致，夜间是「主动打开的」，
 * 不跟随系统 prefers-color-scheme（否则深色系统的同事会突然换一套配色）。
 * 选择存在 localStorage，换浏览器/清缓存后回到白天。
 *
 * 开发预览支持 ?theme=dark|light 强制覆盖，便于同机对照截图。
 */

import { ref } from 'vue'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'powersystem.theme'

function read(): Theme {
  if (import.meta.env.DEV) {
    const forced = new URLSearchParams(window.location.search).get('theme')
    if (forced === 'dark' || forced === 'light') return forced
  }
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'dark' || saved === 'light') return saved
  } catch {
    /* 隐私模式禁写 localStorage：按默认走，不阻断首屏 */
  }
  return 'light'
}

export const theme = ref<Theme>(read())

/** 只改标记，不碰样式：tokens.css 的 :root[data-theme='dark'] 负责换色 */
export function applyTheme(): void {
  document.documentElement.dataset.theme = theme.value
}

export function setTheme(next: Theme): void {
  theme.value = next
  applyTheme()
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* 存不下只影响下次打开时的初值，当前会话仍生效 */
  }
}

export function toggleTheme(): void {
  setTheme(theme.value === 'dark' ? 'light' : 'dark')
}

export function watchTheme(): void {
  applyTheme()
}
