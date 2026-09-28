import { createApp } from 'vue'
import { ElAlert, ElButton, ElButtonGroup, ElDatePicker, ElDrawer, ElIcon, ElInput, ElOption, ElPagination, ElProgress, ElSelect, ElTable, ElTableColumn, ElTag, ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import './styles.css'
import App from './App.vue'
import { router } from './router'

const app = createApp(App)
for (const component of [ElAlert, ElButton, ElButtonGroup, ElDatePicker, ElDrawer, ElIcon, ElInput, ElOption, ElPagination, ElProgress, ElSelect, ElTable, ElTableColumn, ElTag]) app.use(component)
app.directive('loading', ElLoading.directive)
app.use(router).mount('#app')
