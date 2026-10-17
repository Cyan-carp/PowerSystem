import { createApp } from 'vue'
import { ElAlert, ElButton, ElButtonGroup, ElCollapse, ElCollapseItem, ElEmpty, ElDatePicker, ElDialog, ElDrawer, ElIcon, ElInput, ElOption, ElPagination, ElProgress, ElSelect, ElTable, ElTableColumn, ElTag, ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles.css'

document.documentElement.classList.add('dark')
import App from './App.vue'
import { router } from './router'

const app = createApp(App)
for (const component of [ElAlert, ElButton, ElButtonGroup, ElCollapse, ElCollapseItem, ElEmpty, ElDatePicker, ElDialog, ElDrawer, ElIcon, ElInput, ElOption, ElPagination, ElProgress, ElSelect, ElTable, ElTableColumn, ElTag]) app.use(component)
app.directive('loading', ElLoading.directive)
app.use(router).mount('#app')
