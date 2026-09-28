import { createRouter, createWebHistory } from 'vue-router'
import { token } from './lib/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('./views/LoginView.vue'), name: 'login' },
    {
      path: '/', component: () => import('./layouts/ConsoleLayout.vue'), meta: { requiresAuth: true }, children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', component: () => import('./views/DashboardView.vue'), name: 'dashboard' },
        { path: 'devices', component: () => import('./views/DevicesView.vue'), name: 'devices' },
        { path: 'devices/:id', component: () => import('./views/DeviceDetailView.vue'), name: 'device-detail' },
        { path: 'alarms', component: () => import('./views/AlarmsView.vue'), name: 'alarms' },
        { path: 'predictions', component: () => import('./views/PredictionsView.vue'), name: 'predictions' },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

router.beforeEach((to) => {
  if (to.matched.some((record) => record.meta.requiresAuth) && !token.value) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && token.value) return { name: 'dashboard' }
})
