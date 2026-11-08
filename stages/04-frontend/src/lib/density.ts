/**
 * 显示密度自适应：只影响渲染，不碰任何数据与接口。
 *
 * 为什么需要：全站尺寸写的是固定 px（body 13px、卡片标题 14.5px、页边距 22px、
 * 内容上限 1440px）。浏览器按 devicePixelRatio 把这些 px 放大，但每块屏的 PPI 不同——
 * 同一个 13px 在 24 寸 1080p（约 92 PPI）上物理尺寸接近 14 寸 2K（约 200 PPI）的两倍，
 * 于是低分屏显得「字大、卡片贴边」，高分屏显得精致；同时固定 1440px 的居中留白，
 * 在 CSS 视口被系统缩放压窄后会被吃光。
 *
 * 判定用「屏幕物理像素宽」= screen.width(CSS px) × dpr ÷ 页面缩放：
 *   screen.width 只随 Windows 显示缩放变化，dpr = 系统 DPR × 浏览器页面缩放，
 *   用 outerWidth/innerWidth 把页面缩放除掉后，乘积才是稳定的物理分辨率，
 *   不随系统缩放、浏览器缩放或窗口拖窄而漂移，能区分 1080p(≈1920) 与 2K(≈2560)；
 *   视口本身过窄时由 shell.css 的窄屏断点接管，不做二次缩放。
 * 低分屏只把页面内容整体收紧一档；顶栏、图标轨、抽屉与所有 Toast 都不在 .page-inner
 * 内（浮层 Teleport 到 body），不受缩放影响。缩放系数由 shell.css 的 --content-scale 定义。
 *
 * 开发预览支持 ?density=compact|standard 强制覆盖，便于同机对照截图；生产始终自动。
 */

/** 小于这个物理像素宽视为低像素密度屏（1080p 级） */
const COMPACT_MAX_PHYSICAL_WIDTH = 2200

export type Density = 'compact' | 'standard'

function detect(): Density {
  const dpr = window.devicePixelRatio || 1
  const screenCssWidth = window.screen?.width || window.innerWidth
  /* 窗口外框宽不随页面缩放变化，视口宽会；两者之比即页面缩放（含边框/滚动条少量误差） */
  const pageZoom =
    window.outerWidth > 0 && window.innerWidth > 0
      ? window.outerWidth / window.innerWidth
      : 1
  const physicalWidth = (screenCssWidth * dpr) / Math.max(pageZoom, 0.5)
  /* 与 shell.css 的 max-width:1100px 断点对齐：视口窄时不叠加缩放 */
  if (window.innerWidth < 1100 || physicalWidth >= COMPACT_MAX_PHYSICAL_WIDTH) return 'standard'
  return 'compact'
}

export function applyDensity(): void {
  let density: Density = detect()
  if (import.meta.env.DEV) {
    const forced = new URLSearchParams(window.location.search).get('density')
    if (forced === 'compact' || forced === 'standard') density = forced
  }
  document.documentElement.dataset.density = density
}

let dprQuery: MediaQueryList | null = null

/** 浏览器缩放 / 跨屏拖动时 DPR 会变，但未必触发 resize，单独兜一层 */
function bindDprWatch(): void {
  dprQuery?.removeEventListener('change', onDprChange)
  dprQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`)
  dprQuery.addEventListener('change', onDprChange)
}

function onDprChange(): void {
  applyDensity()
  bindDprWatch()
}

export function watchDensity(): void {
  applyDensity()
  window.addEventListener('resize', applyDensity, { passive: true })
  bindDprWatch()
}
