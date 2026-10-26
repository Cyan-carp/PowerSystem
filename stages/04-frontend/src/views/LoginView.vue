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
    <section class="login-visual" aria-hidden="true">
      <PowerGridScene />
      <header class="login-visual-head">
        <span class="brand-orb">
          <svg viewBox="0 0 16 16"><path d="M9.4 1.2 3.5 8.8h3.2l-1 6 5.8-7.7H8.3Z" fill="#fff" /></svg>
        </span>
        <div>
          <strong>源网智联</strong>
          <small>新能源设备智能运维平台</small>
        </div>
      </header>

      <div class="login-visual-body">
        <h1>设备、告警与功率曲线<br>收在同一张<em>控制台</em>里</h1>
        <p>遥测采集、实时告警、健康评分、故障预测与值班问答共用一条链路。登录后按场站与设备范围查看当前运行状态，缺测与过期结果单独标识，不按零计算。</p>
        <div class="login-metrics">
          <div class="login-metric"><span>接入设备</span><strong>143<em>台</em></strong></div>
          <div class="login-metric"><span>采样周期</span><strong>5<em>秒</em></strong></div>
          <div class="login-metric"><span>产物保留</span><strong>7<em>天</em></strong></div>
        </div>
      </div>

      <footer class="login-visual-foot">
        <span>遥测接入</span>
        <span>实时告警</span>
        <span>故障预测</span>
        <span>值班问答</span>
      </footer>
    </section>

    <section class="login-panel">
      <div class="login-stage">
        <div class="login-status">
          <span class="login-status-dot" />
          数据链路正常
        </div>
        <h2>登录控制台</h2>
        <p class="card-sub">使用已注册的运维账号进入控制台。</p>

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
          <el-button native-type="submit" type="primary" size="large" :loading="busy" class="login-submit">登录</el-button>
        </form>

        <div class="login-footnote">平台业务接口使用 JWT 鉴权 · 登录有效期 8 小时 · 凭据只保存在当前标签页</div>
        <div class="login-meta">
          <span>只读控制台</span>
          <span>写动作需审批</span>
          <span>合成数据环境</span>
        </div>
      </div>
    </section>
  </div>
</template>
