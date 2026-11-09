/**
 * 站点级信息：主要是备案号。
 *
 * 域名入口会在返回的 HTML 末尾注入 `<footer class="icp-footer">`（带内联样式），IP 入口没有。
 * 以前把它改成一条固定在屏幕底部的 26px 黑条，手机上还要给它和底部导航同时让位。
 * 现在改为：启动时把这段节点**搬进应用内**——从文档流里摘掉，内容与链接原样保留在
 * `icpNotice` 里，由页面自己在滚动到底时渲染（总览页底部与「关于」页）。
 *
 * 这样做有两个好处：
 * - 不再有常驻底栏，手机上的底部导航独占底部；
 * - 备案号与工信部链接仍来自服务端注入的同一段 HTML，前端不复制一份号码。
 */
import { ref } from 'vue'

export interface IcpNotice {
  /** 注入节点的 innerHTML（含 <a>），原样渲染 */
  html: string
  /** 工信部链接，便于「关于」页单独使用 */
  href: string
  /** 纯文本，用于 title 等 */
  text: string
}

export const icpNotice = ref<IcpNotice | null>(null)

/** 在挂载前调用一次：摘掉服务端注入的页脚，保留其内容 */
export function captureIcpNotice(): void {
  if (typeof document === 'undefined') return
  const node = document.querySelector('.icp-footer')
  if (!node) return
  const link = node.querySelector('a')
  icpNotice.value = {
    html: node.innerHTML.trim(),
    href: link?.getAttribute('href') || 'https://beian.miit.gov.cn/',
    text: (node.textContent || '').trim(),
  }
  node.remove()
}

/** 当前前端构建标识（Vite 产物名里的内容哈希），用于确认手机上是不是新版 */
export function buildTag(): string {
  if (typeof document === 'undefined') return ''
  const scripts = Array.from(document.querySelectorAll('script[src]')) as HTMLScriptElement[]
  for (const script of scripts) {
    const match = /\/assets\/(index-[A-Za-z0-9_-]+)\.js/.exec(script.src)
    if (match) return match[1]
  }
  return ''
}
