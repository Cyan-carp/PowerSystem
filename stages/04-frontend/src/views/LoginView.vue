<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../lib/api'
import { saveSession } from '../lib/auth'
import { errorMessage } from '../lib/format'
import PowerGridScene from '../components/PowerGridScene.vue'

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
    <PowerGridScene />
    <div class="login-scrim" aria-hidden="true" />

    <div class="login-stage">
      <header class="login-brand">
        <span class="login-mark" aria-hidden="true">
          <svg viewBox="0 0 40 40">
            <path class="mark-hex" d="M20 2.6 34.9 11.3v17.4L20 37.4 5.1 28.7V11.3Z" />
            <path class="mark-bolt" d="M22 11.6 13.8 22.1h4.8l-1.7 6.7 7.9-10.3h-4.9Z" />
          </svg>
        </span>
        <h1>源网智联</h1>
        <p>新能源设备智能运维平台</p>
      </header>

      <section class="login-card">
        <div class="login-status">
          <span class="login-status-dot" />
          输电走廊数据链路正常
        </div>
        <h2>登录平台</h2>
        <p class="card-sub">使用已注册的运维账号进入系统。</p>

        <form @submit.prevent="submit">
          <div class="login-field">
            <label for="username">账号</label>
            <el-input id="username" v-model="form.username" autocomplete="username" size="large" placeholder="输入账号" />
          </div>
          <div class="login-field">
            <label for="password">密码</label>
            <el-input id="password" v-model="form.password" type="password" autocomplete="current-password" show-password size="large" placeholder="输入密码" />
          </div>
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <el-button native-type="submit" type="primary" size="large" :loading="busy" class="login-submit">进入工作台</el-button>
        </form>

        <div class="login-footnote">平台业务接口使用 JWT 鉴权 · 登录有效期 8 小时</div>
      </section>

      <footer class="login-meta">
        <span>遥测接入</span>
        <span>实时告警</span>
        <span>故障预测</span>
        <span>值班问答</span>
      </footer>
    </div>
  </div>
</template>
