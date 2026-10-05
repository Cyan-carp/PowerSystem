import { createRouter, createWebHistory } from 'vue-router'
import { token } from './lib/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('./views/LoginView.vue'), name: 'login' },
    {
      path: '/', component: () => import('./layouts/ConsoleLayout.vue'), meta: { requiresAuth: true }, children: [
        { path: 'agent', component: () => import('./views/AgentChatView.vue'), name: 'agent-chat', meta: { title: '值班问答' } },
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', component: () => import('./views/DashboardView.vue'), name: 'dashboard', meta: { title: '运行总览' } },
        { path: 'devices', component: () => import('./views/DevicesView.vue'), name: 'devices', meta: { title: '设备管理' } },
        { path: 'devices/:id', component: () => import('./views/DeviceDetailView.vue'), name: 'device-detail', meta: { title: '设备详情' } },
        { path: 'alarms', component: () => import('./views/AlarmsView.vue'), name: 'alarms', meta: { title: '告警中心' } },
        { path: 'predictions', component: () => import('./views/PredictionsView.vue'), name: 'predictions', meta: { title: '故障预测' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

router.beforeEach((to) => {
  if (to.matched.some((record) => record.meta.requiresAuth) && !token.value) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && token.value) return { name: 'dashboard', meta: { title: '运行总览' } }
})
