<script setup lang="ts">
/**
 * 登录页：与会话层（lib/auth.ts）配套的唯一入口。
 *
 * 视觉只用 tokens.css 的令牌与 shell.css 的控件口径（白卡 + 浅底 + 8px 圆角 + 品牌蓝），
 * 不复用旧前端那套 Element Plus 大圆角与深色分栏。
 *
 * 口径：
 * - 账号密码错（401）留在本页显示行内错误，不走全局 401 踢出逻辑（见 api.login 的注释）。
 * - 左侧三个数字是系统契约里的常量，不是演示数据。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import { ApiError, api } from '../lib/api'
import { lastUsername, saveSession } from '../lib/auth'
import { toast } from '../lib/toast'

const route = useRoute()
const router = useRouter()

const form = reactive({ username: lastUsername(), password: '' })
const busy = ref(false)
const error = ref('')
const reveal = ref(false)

/** 链路口径：登录前唯一能免鉴权调的就是 /ping，用它判断而不是写死“正常” */
const link = ref<'checking' | 'ok' | 'bad'>('checking')
const linkText = computed(() =>
  link.value === 'checking' ? '检测中' : link.value === 'ok' ? '链路正常' : '链路异常',
)

const probe = async (): Promise<void> => {
  link.value = 'checking'
  try {
    await api.ping()
    link.value = 'ok'
  } catch {
    link.value = 'bad'
  }
}

/** 只接受站内路径，挡掉 //evil.com 这类开放跳转 */
const redirectTarget = (): string => {
  const raw = route.query.redirect
  if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//')) return '/'
  return raw
}

const usernameInput = ref<HTMLInputElement | null>(null)

const submit = async (): Promise<void> => {
  error.value = ''
  if (!form.username.trim()) {
    error.value = '请输入账号'
    usernameInput.value?.focus()
    return
  }
  if (!form.password) {
    error.value = '请输入密码'
    return
  }
  busy.value = true
  try {
    saveSession(await api.login(form.username.trim(), form.password))
    toast('ok', '登录成功')
    await router.replace(redirectTarget())
  } catch (cause) {
    error.value =
      cause instanceof ApiError && cause.message ? cause.message : '登录失败，请稍后重试'
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void probe()
})
</script>

<template>
  <div class="login">
    <div class="login-frame">
      <!-- 左：品牌与口径，直接落在页面底色上，不套卡片 -->
      <section class="intro">
        <header class="intro-brand">
          <span class="mark">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M9.4 1.2 3.5 8.8h3.2l-1 6 5.8-7.7H8.3Z" fill="#fff" />
            </svg>
          </span>
          <strong>源网智联</strong>
        </header>

        <h1>新能源设备智能运维平台</h1>
        <p class="intro-tags">遥测接入 · 实时告警 · 健康评分 · 故障预测</p>

        <ul class="intro-facts">
          <li>
            <span>采样周期</span>
            <b class="num">5<i>秒</i></b>
          </li>
          <li>
            <span>登录有效期</span>
            <b class="num">8<i>小时</i></b>
          </li>
          <li>
            <span>产物保留</span>
            <b class="num">7<i>天</i></b>
          </li>
        </ul>
      </section>

      <!-- 右：登录卡 -->
      <section class="panel">
        <header class="panel-head">
          <h2>登录控制台</h2>
          <span class="link-chip" :class="`is-${link}`">
            <i></i>{{ linkText }}
          </span>
        </header>

        <form class="panel-form" novalidate @submit.prevent="submit">
          <label class="f-label" for="username">账号</label>
          <div class="f-box" :class="{ 'is-bad': error && !form.username.trim() }">
            <Icon name="user" :size="15" />
            <input
              id="username"
              ref="usernameInput"
              v-model="form.username"
              type="text"
              autocomplete="username"
              placeholder="输入账号"
              spellcheck="false"
              @input="error = ''"
            />
          </div>

          <label class="f-label" for="password">密码</label>
          <div class="f-box" :class="{ 'is-bad': error && form.username.trim() && !form.password }">
            <Icon name="lock" :size="15" />
            <input
              id="password"
              v-model="form.password"
              :type="reveal ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="输入密码"
              @input="error = ''"
            />
            <button
              type="button"
              class="f-eye"
              :title="reveal ? '隐藏密码' : '显示密码'"
              :aria-label="reveal ? '隐藏密码' : '显示密码'"
              :aria-pressed="reveal"
              @click="reveal = !reveal"
            >
              <Icon :name="reveal ? 'eyeOff' : 'eye'" :size="15" />
            </button>
          </div>

          <p v-if="error" class="f-error" role="alert">
            <i></i>{{ error }}
          </p>

          <button type="submit" class="f-submit" :disabled="busy">
            <span v-if="busy" class="spin" aria-hidden="true"></span>
            {{ busy ? '正在登录' : '登录' }}
          </button>
        </form>

        <p class="login-note">
          <Icon name="lock" :size="12" />
          JWT 鉴权 · 8 小时有效
        </p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.login {
  position: relative;
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 40px 24px;
  overflow: hidden;
}

/* 顶部一层品牌微光：只提亮左上，不铺满，避免整页发蓝 */
.login::before {
  content: '';
  position: absolute;
  inset: -12% -10% auto -10%;
  height: 620px;
  background: radial-gradient(680px 320px at 24% 0%, rgba(37, 99, 235, 0.14), transparent 72%);
  pointer-events: none;
}

/* 点阵网格，向上淡出；纯装饰，不承载信息 */
.login::after {
  content: '';
  position: absolute;
  inset: 0;
  background-image: radial-gradient(rgba(15, 23, 42, 0.06) 1px, transparent 1px);
  background-size: 22px 22px;
  -webkit-mask-image: radial-gradient(880px 520px at 32% 6%, #000 0%, transparent 74%);
  mask-image: radial-gradient(880px 520px at 32% 6%, #000 0%, transparent 74%);
  pointer-events: none;
}

.login-frame {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 1000px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 372px;
  gap: 64px;
  align-items: center;
}

/* ---------- 左：品牌与口径 ---------- */

.intro-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.intro-brand strong {
  font-size: 14.5px;
  font-weight: 650;
  letter-spacing: 0.02em;
  color: var(--ink);
}

.mark {
  width: 32px;
  height: 32px;
  border-radius: var(--r-lg);
  display: grid;
  place-items: center;
  background: linear-gradient(142deg, #3b82f6 0%, var(--brand-deep) 100%);
  box-shadow: 0 4px 12px -2px var(--brand-glow);
  flex: 0 0 32px;
}

.mark svg {
  width: 16px;
  height: 16px;
}

.intro h1 {
  margin-top: 30px;
  font-size: 29px;
  line-height: 1.32;
  font-weight: 650;
  letter-spacing: -0.024em;
  color: var(--ink);
}

.intro-tags {
  margin-top: 12px;
  font-size: 12px;
  letter-spacing: 0.02em;
  color: var(--ink-4);
}

.intro-facts {
  margin: 32px 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  max-width: 380px;
  border-top: 1px solid var(--line-strong);
}

.intro-facts li {
  padding: 14px 0 0 16px;
  border-left: 1px solid var(--line-strong);
}

.intro-facts li:first-child {
  border-left: 0;
  padding-left: 0;
}

.intro-facts span {
  display: block;
  font-size: 11px;
  color: var(--ink-4);
  margin-bottom: 5px;
}

.intro-facts b {
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--ink);
}

.intro-facts i {
  font-style: normal;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--ink-4);
  margin-left: 3px;
}

/* ---------- 右：登录卡 ---------- */

.panel {
  background: var(--card);
  border: 1px solid #e7ebf3;
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-2);
  padding: 20px 24px 20px;
}

.panel-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-head h2 {
  margin-right: auto;
}

.link-chip {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 21px;
  padding: 0 8px;
  border-radius: var(--r-sm);
  font-size: 10.5px;
  font-weight: 600;
  background: #f1f5f9;
  color: #64748b;
}.link-chip i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.link-chip.is-ok {
  background: var(--ok-soft);
  color: #047857;
}

.link-chip.is-bad {
  background: var(--bad-soft);
  color: var(--bad-deep);
}

.panel h2 {
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.015em;
  color: var(--ink);
}

.panel-form {
  margin-top: 20px;
}

.f-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
}

.f-label:not(:first-child) {
  margin-top: 14px;
}

.f-box {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 40px;
  padding: 0 11px;
  border-radius: var(--r-md);
  border: 1px solid var(--line-strong);
  background: #fff;
  color: var(--ink-placeholder);
  transition: border-color 0.16s ease, box-shadow 0.16s ease, color 0.16s ease;
}

.f-box:focus-within {
  border-color: var(--brand-line);
  box-shadow: 0 0 0 3px var(--brand-soft);
  color: var(--brand);
}

.f-box.is-bad {
  border-color: #f3b4b4;
  color: var(--bad);
}

.f-box input {
  flex: 1 1 auto;
  min-width: 0;
  border: 0;
  outline: none;
  background: transparent;
  font-size: 13px;
  color: var(--ink);
}

.f-box input::placeholder {
  color: var(--ink-placeholder);
}

/* 浏览器自带的密码眼睛与补全背景要压掉，否则和自绘样式打架 */
.f-box input:-webkit-autofill {
  -webkit-text-fill-color: var(--ink);
  box-shadow: 0 0 0 40px #fff inset;
}

.f-eye {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  border-radius: var(--r-sm);
  display: grid;
  place-items: center;
  color: var(--ink-4);
}

.f-eye:hover {
  color: var(--ink-2);
  background: var(--card-soft);
}

.f-error {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: var(--r-sm);
  background: var(--bad-soft);
  color: var(--bad-deep);
  font-size: 11.5px;
  line-height: 1.5;
}

.f-error i {
  flex: 0 0 5px;
  width: 5px;
  height: 5px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--bad);
}

.f-submit {
  margin-top: 18px;
  width: 100%;
  height: 42px;
  border-radius: var(--r-md);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--brand);
  color: #fff;
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
  box-shadow: 0 6px 16px -7px var(--brand-glow), 0 1px 2px rgba(16, 24, 40, 0.12);
  transition: background 0.16s ease, box-shadow 0.16s ease, transform 0.12s ease;
}

.f-submit:hover:not(:disabled) {
  background: var(--brand-deep);
  box-shadow: 0 8px 20px -7px var(--brand-glow);
}

.f-submit:active:not(:disabled) {
  transform: translateY(1px);
}

.f-submit:disabled {
  opacity: 0.72;
  cursor: progress;
}

.spin {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.45);
  border-top-color: #fff;
  animation: spin 0.72s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.login-note {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 12px;
  font-size: 10.5px;
  color: var(--ink-4);
}

/* ---------- 窄屏：单列，卡片在下 ---------- */

@media (max-width: 900px) {
  .login {
    padding: 28px 16px;
  }

  .login-frame {
    grid-template-columns: minmax(0, 1fr);
    gap: 24px;
    max-width: 400px;
  }

  .intro h1 {
    margin-top: 20px;
    font-size: 22px;
  }

  .intro-tags {
    margin-top: 8px;
    font-size: 11.5px;
  }

  .intro-facts {
    margin-top: 22px;
  }

  .intro-facts b {
    font-size: 19px;
  }

  .panel {
    padding: 18px 18px 18px;
  }
}
</style>
