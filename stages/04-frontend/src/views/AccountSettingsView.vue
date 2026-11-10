<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import Icon from '../components/Icon.vue'
import { ApiError, api } from '../lib/api'
import { clearSession, type AuthUser } from '../lib/auth'
import { toast } from '../lib/toast'

const router = useRouter()
const account = ref<AuthUser | null>(null)
const loading = ref(true)
const loadError = ref('')
const form = reactive({ current: '', next: '', confirm: '' })
const reveal = ref(false)
const busy = ref(false)
const error = ref('')
const currentInput = ref<HTMLInputElement | null>(null)
const role = computed(() => account.value?.role === 'admin' ? '管理员' : '运维员')
const users = ref<AuthUser[]>([])
const canDeleteAdmin = ref(false)
const usersLoading = ref(false)
const usersError = ref('')
const creating = ref(false)
const deleting = ref<number | null>(null)
const createError = ref('')
const createForm = reactive({ username: '', real_name: '', password: '', confirm: '', role: 'operator' as 'operator' | 'admin' })

async function loadUsers(): Promise<void> {
  if (account.value?.role !== 'admin') return
  usersLoading.value = true
  usersError.value = ''
  try {
    const data = await api.users()
    users.value = data.list
    canDeleteAdmin.value = data.can_delete_admin
  } catch (cause) {
    usersError.value = cause instanceof ApiError ? cause.message : '账号列表暂时无法读取'
  } finally {
    usersLoading.value = false
  }
}

async function createUser(): Promise<void> {
  if (creating.value) return
  createError.value = ''
  const username = createForm.username.trim()
  const bytes = new TextEncoder().encode(createForm.password).length
  if (!/^[A-Za-z0-9_-]{3,64}$/.test(username)) {
    createError.value = '账号须为 3–64 位字母、数字、下划线或短横线'
  } else if (bytes < 8 || bytes > 72) {
    createError.value = '密码须为 8–72 字节'
  } else if (createForm.password !== createForm.confirm) {
    createError.value = '两次输入的密码不一致'
  }
  if (createError.value) return
  creating.value = true
  try {
    await api.registerUser({ username, real_name: createForm.real_name.trim(), password: createForm.password, role: createForm.role })
    createForm.username = ''
    createForm.real_name = ''
    createForm.password = ''
    createForm.confirm = ''
    createForm.role = 'operator'
    toast('ok', '账号已创建，请单独交付登录信息')
    await loadUsers()
  } catch (cause) {
    createError.value = cause instanceof ApiError && cause.status === 409 ? '账号已存在或暂不可用' :
      cause instanceof ApiError ? cause.message : '创建失败，请稍后重试'
  } finally {
    creating.value = false
  }
}

async function removeUser(user: AuthUser): Promise<void> {
  if (deleting.value !== null || user.id === account.value?.id) return
  if (!window.confirm(`确认删除账号 ${user.username}？该账号的登录会话会失效，历史业务记录会保留。`)) return
  deleting.value = user.id
  usersError.value = ''
  try {
    await api.deleteUser(user.id)
    toast('ok', `账号 ${user.username} 已删除`)
    await loadUsers()
  } catch (cause) {
    usersError.value = cause instanceof ApiError ? cause.message : '删除失败，请稍后重试'
  } finally {
    deleting.value = null
  }
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    account.value = await api.me()
    if (account.value.role === 'admin') await loadUsers()
  } catch (cause) {
    loadError.value = cause instanceof ApiError ? cause.message : '账号资料暂时无法读取'
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''
  const bytes = new TextEncoder().encode(form.next).length
  if (!form.current) {
    error.value = '请输入当前密码'
    currentInput.value?.focus()
    return
  }
  if (bytes < 8 || bytes > 72) {
    error.value = '新密码长度须为 8–72 字节'
    return
  }
  if (form.next !== form.confirm) {
    error.value = '两次输入的新密码不一致'
    return
  }
  if (form.current === form.next) {
    error.value = '新密码不能与当前密码相同'
    return
  }
  busy.value = true
  try {
    await api.changePassword(form.current, form.next)
    form.current = ''
    form.next = ''
    form.confirm = ''
    clearSession()
    toast('ok', '密码已修改，请在所有设备重新登录')
    await router.replace('/login')
  } catch (cause) {
    if (cause instanceof ApiError) {
      error.value = cause.code === 40002 ? '当前密码不正确' :
        cause.code === 40003 ? '新密码不能与当前密码相同' :
        cause.status === 409 ? '密码已在其他设备修改，请重新登录' :
        cause.status === 0 ? '无法连接后端服务，请稍后重试' :
        cause.message || '修改失败，请稍后重试'
    } else {
      error.value = '修改失败，请稍后重试'
    }
  } finally {
    busy.value = false
  }
}

onMounted(() => { void load() })
</script>

<template>
  <div class="account-layout">
    <section class="card">
      <div class="card-head"><h2 class="card-title">本人资料</h2></div>
      <div class="card-body">
        <p v-if="loading" class="state" role="status">正在读取账号资料…</p>
        <div v-else-if="loadError" class="state">
          <p role="alert">{{ loadError }}</p>
          <button type="button" class="btn btn-sm" @click="load">重试</button>
        </div>
        <dl v-else-if="account" class="account-facts">
          <dt>账号</dt><dd>{{ account.username }}</dd>
          <dt>姓名</dt><dd>{{ account.real_name || '未设置' }}</dd>
          <dt>角色</dt><dd>{{ role }}</dd>
        </dl>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h2 class="card-title">修改密码</h2></div>
      <div class="card-body">
        <p class="explain">修改成功后，此账号在所有设备上的旧登录会话都会失效。</p>
        <form class="password-form" novalidate @submit.prevent="submit">
          <label for="current-password">当前密码</label>
          <input id="current-password" ref="currentInput" v-model="form.current" class="field" :type="reveal ? 'text' : 'password'" autocomplete="current-password" :disabled="busy" @input="error = ''" />
          <label for="new-password">新密码</label>
          <input id="new-password" v-model="form.next" class="field" :type="reveal ? 'text' : 'password'" autocomplete="new-password" :disabled="busy" aria-describedby="password-rule" @input="error = ''" />
          <p id="password-rule" class="hint">8–72 字节；中文字符可能占多个字节。</p>
          <label for="confirm-password">确认新密码</label>
          <input id="confirm-password" v-model="form.confirm" class="field" :type="reveal ? 'text' : 'password'" autocomplete="new-password" :disabled="busy" @input="error = ''" />
          <label class="reveal">
            <input v-model="reveal" type="checkbox" :disabled="busy" />
            <Icon :name="reveal ? 'eyeOff' : 'eye'" :size="15" />
            {{ reveal ? '隐藏密码' : '显示密码' }}
          </label>
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <button type="submit" class="btn btn-primary submit" :disabled="busy || loading || !!loadError">
            {{ busy ? '正在修改…' : '修改密码' }}
          </button>
        </form>
      </div>
    </section>

    <section v-if="account?.role === 'admin'" class="card management">
      <div class="card-head"><h2 class="card-title">创建账号</h2></div>
      <div class="card-body">
        <p class="explain">只有管理员可创建账号并指定权限。新管理员可管理普通运维人员；只有原始 user 账号可删除管理员。</p>
        <form class="password-form" novalidate @submit.prevent="createUser">
          <label for="register-username">新账号</label>
          <input id="register-username" v-model="createForm.username" class="field" autocomplete="off" :disabled="creating" @input="createError = ''" />
          <label for="register-name">姓名</label>
          <input id="register-name" v-model="createForm.real_name" class="field" autocomplete="off" maxlength="64" :disabled="creating" @input="createError = ''" />
          <label for="register-role">账号权限</label>
          <select id="register-role" v-model="createForm.role" class="field" :disabled="creating">
            <option value="operator">常规运维人员</option>
            <option value="admin">管理员</option>
          </select>
          <label for="register-password">初始密码</label>
          <input id="register-password" v-model="createForm.password" class="field" type="password" autocomplete="new-password" :disabled="creating" @input="createError = ''" />
          <p class="hint">8–72 字节；密码只用于本次创建，不会保存在页面地址或本地存储。</p>
          <label for="register-confirm">确认密码</label>
          <input id="register-confirm" v-model="createForm.confirm" class="field" type="password" autocomplete="new-password" :disabled="creating" @input="createError = ''" />
          <p v-if="createError" class="form-error" role="alert">{{ createError }}</p>
          <button type="submit" class="btn btn-primary submit" :disabled="creating">{{ creating ? '正在创建…' : '创建账号' }}</button>
        </form>
      </div>
    </section>

    <section v-if="account?.role === 'admin'" class="card management">
      <div class="card-head"><h2 class="card-title">账号管理</h2></div>
      <div class="card-body">
        <p v-if="usersLoading" class="state" role="status">正在读取账号…</p>
        <p v-if="usersError" class="form-error" role="alert">{{ usersError }}</p>
        <button v-if="usersError" type="button" class="btn btn-sm" @click="loadUsers">重试</button>
        <ul v-if="!usersLoading" class="user-list">
          <li v-for="user in users" :key="user.id" class="user-row">
            <span><strong>{{ user.username }}</strong><small>{{ user.real_name || '未设置姓名' }} · {{ user.role === 'admin' ? '管理员' : '常规运维人员' }}</small></span>
            <button v-if="user.id !== account?.id && (user.role !== 'admin' || canDeleteAdmin)" type="button" class="btn btn-sm" :disabled="deleting !== null" :aria-label="`删除账号 ${user.username}`" @click="removeUser(user)">删除</button>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
.account-layout { display: grid; grid-template-columns: minmax(240px, 0.8fr) minmax(320px, 1.2fr); align-items: start; gap: 18px; max-width: 920px; }
.state { color: var(--ink-3); font-size: 12.5px; line-height: 1.6; }
.state .btn { margin-top: 12px; }
.account-facts { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 14px 12px; margin: 0; font-size: 12.5px; }
.account-facts dt { color: var(--ink-3); }
.account-facts dd { margin: 0; color: var(--ink); font-weight: 600; overflow-wrap: anywhere; }
.explain { margin: 0 0 17px; color: var(--ink-3); font-size: 12.5px; line-height: 1.6; }
.password-form { display: grid; gap: 8px; max-width: 420px; }
.password-form > label:not(.reveal) { color: var(--ink-2); font-size: 12px; font-weight: 600; margin-top: 6px; }
.password-form .field { width: 100%; min-width: 0; height: 38px; }
.hint { margin: 0; font-size: 11.5px; color: var(--ink-3); }
.reveal { display: inline-flex; align-items: center; gap: 7px; width: fit-content; margin: 6px 0; color: var(--ink-2); font-size: 12px; cursor: pointer; }
.reveal input { accent-color: var(--brand); }
.form-error { margin: 0; padding: 9px 11px; border: 1px solid var(--err-line); border-radius: var(--r-md); background: var(--bad-soft); color: var(--bad-deep); font-size: 12px; }
.submit { justify-self: start; min-width: 110px; }
.management { grid-column: 1 / -1; }
.user-list { list-style: none; margin: 0; padding: 0; }
.user-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 11px 0; border-bottom: 1px solid var(--line); }
.user-row span { display: grid; gap: 3px; min-width: 0; overflow-wrap: anywhere; font-size: 12.5px; }
.user-row small { color: var(--ink-3); font-size: 11.5px; }
@media (max-width: 760px) { .account-layout { grid-template-columns: 1fr; } .password-form { max-width: none; } }
</style>
