<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lightning } from '@element-plus/icons-vue'
import { api } from '../lib/api'
import { saveSession } from '../lib/auth'
import { errorMessage } from '../lib/format'

const route = useRoute()
const router = useRouter()
const form = reactive({ username: '', password: '' })
const busy = ref(false)
const error = ref('')

async function submit(): Promise<void> {
  error.value = ''
  if (!form.username.trim() || !form.password) { error.value = '请输入账号和密码'; return }
  busy.value = true
  try {
    saveSession(await api.login(form.username.trim(), form.password))
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}
</script>

<template>
  <div class="login-screen">
    <div class="login-identity">
      <div class="mesh" aria-hidden="true" />
      <div class="grid-dots" aria-hidden="true" />
      <div class="inner">
        <div class="login-logo"><el-icon><Lightning /></el-icon></div>
        <span class="eyebrow">POWER SYSTEM OPERATIONS</span>
        <h1>每一台设备的状态，<br />都值得被<b>及时看见</b>。</h1>
        <p>源网智联新能源设备智能运维平台</p>
        <div class="login-line"><span />遥测接入 · 实时告警 · 故障预测 · 值班问答</div>
      </div>
    </div>
    <div class="login-panel">
      <div class="login-form-wrap">
        <span class="section-kicker">DUTY CONSOLE</span>
        <h2>登录平台</h2>
        <p>使用已注册的运维账号进入系统。</p>
        <form @submit.prevent="submit">
          <label for="username">账号</label>
          <el-input id="username" v-model="form.username" autocomplete="username" size="large" placeholder="输入账号" />
          <label for="password">密码</label>
          <el-input id="password" v-model="form.password" type="password" autocomplete="current-password" show-password size="large" placeholder="输入密码" />
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <el-button native-type="submit" type="primary" size="large" :loading="busy" class="login-submit">进入工作台</el-button>
        </form>
        <div class="login-footnote">平台业务接口使用 JWT 鉴权 · 登录有效期 8 小时</div>
      </div>
    </div>
  </div>
</template>
