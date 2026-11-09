import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import { authToken, setUnauthorizedHandler } from './lib/auth'
import { toast } from './lib/toast'
import { watchDensity } from './lib/density'
import { watchTheme } from './lib/theme'
import { captureIcpNotice } from './lib/site'
import './styles/tokens.css'
import './styles/shell.css'

/* 低像素密度屏（1080p 级）把页面内容收紧一档，抵消固定 px 被放大后的「字大、贴边」 */
watchDensity()

/* 主题标记要在挂载前就打上：晚一步会先闪一帧白底 */
watchTheme()

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('./views/LoginView.vue'),
      meta: { label: '登录', public: true },
    },
    { path: '/', name: 'overview', component: () => import('./views/OverviewView.vue'), meta: { label: '场站总览' } },
    { path: '/alarms', name: 'alarms', component: () => import('./views/AlarmsView.vue'), meta: { label: '告警中心' } },
    { path: '/devices', name: 'devices', component: () => import('./views/DevicesView.vue'), meta: { label: '设备台账' } },
    { path: '/devices/:id', name: 'device-detail', component: () => import('./views/DeviceDetailView.vue'), meta: { label: '设备详情' } },
    { path: '/health', name: 'health', component: () => import('./views/HealthView.vue'), meta: { label: '健康排行' } },
    { path: '/predictions', name: 'predictions', component: () => import('./views/PredictionsView.vue'), meta: { label: '故障预测' } },
    { path: '/model-quality', name: 'model-quality', component: () => import('./views/ModelQualityView.vue'), meta: { label: '模型质量' } },
    { path: '/reports', name: 'reports', component: () => import('./views/ReportsView.vue'), meta: { label: '统计报表' } },
    { path: '/schedules', name: 'schedules', component: () => import('./views/SchedulesView.vue'), meta: { label: '定期任务' } },
    { path: '/assistant', name: 'assistant', component: () => import('./views/AssistantView.vue'), meta: { label: '智能助手' } },
    { path: '/about', name: 'about', component: () => import('./views/AboutView.vue'), meta: { label: '关于' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

/** 站内跳转白名单：只认单斜杠开头的路径，挡掉 //evil.com 这类协议相对地址 */
function safeRedirect(raw: unknown): string {
  if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//')) return '/'
  if (raw.startsWith('/login')) return '/'
  return raw
}

router.beforeEach((to) => {
  if (to.meta.public === true) {
    /* 已登录还去登录页：直接回它原来想去的页面 */
    if (to.name === 'login' && authToken.value) return { path: safeRedirect(to.query.redirect) }
    return true
  }
  if (!authToken.value) {
    return { path: '/login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  const label = (to.meta.label as string) || '控制台'
  document.title = `${label} · 源网智联`
})

/* 401 的唯一出口：清会话（auth 层已做）→ toast → 带 redirect 回登录页。
   这里用地址栏而不是 router.currentRoute：首屏 401 时路由首次导航还没落定，currentRoute 仍是 '/'，
   用它会把用户送回首页而不是他打开的那一页。 */
setUnauthorizedHandler(() => {
  const here = `${window.location.pathname}${window.location.search}`
  if (here.startsWith('/login')) return
  toast('warn', '登录已失效，请重新登录')
  void router.replace({ path: '/login', query: here === '/' ? {} : { redirect: here } })
})

/* 域名入口会在 HTML 末尾注入备案页脚：启动时把它从文档流摘出来、内容留进 icpNotice，
   再由页面在滚动到底时渲染成普通页脚（IP 入口无页脚，不受影响）。 */
captureIcpNotice()

createApp(App).use(router).mount('#app')
