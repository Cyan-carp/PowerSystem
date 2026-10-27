import { createApp } from 'vue'
import { ElAlert, ElButton, ElButtonGroup, ElCheckbox, ElCollapse, ElCollapseItem, ElEmpty, ElDatePicker, ElDialog, ElDrawer, ElIcon, ElInput, ElOption, ElPagination, ElProgress, ElSelect, ElTable, ElTableColumn, ElTag, ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import './styles.css'
import App from './App.vue'
import { router } from './router'
import { initDesktop } from './lib/desktop'
import { token } from './lib/auth'
import { realtime } from './lib/realtime'

void initDesktop()
window.addEventListener('powersystem-desktop-alarm', event => {
  const id = (event as CustomEvent).detail
  if (token.value && Number.isSafeInteger(id) && id > 0) void router.push({ path: '/alarms', query: { id: String(id) } })
})
window.addEventListener('powersystem-desktop-resume', () => realtime.recover())
window.addEventListener('online', () => realtime.recover())

const app = createApp(App)
for (const component of [ElAlert, ElButton, ElButtonGroup, ElCheckbox, ElCollapse, ElCollapseItem, ElEmpty, ElDatePicker, ElDialog, ElDrawer, ElIcon, ElInput, ElOption, ElPagination, ElProgress, ElSelect, ElTable, ElTableColumn, ElTag]) app.use(component)
app.directive('loading', ElLoading.directive)
app.use(router).mount('#app')
