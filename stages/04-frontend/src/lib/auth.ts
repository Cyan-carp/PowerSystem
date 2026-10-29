/**
 * 会话层：token、当前用户与角色，全局只有这一份。
 *
 * 约定：
 * - 业务代码不直接读 localStorage，一律用这里的 `authToken` / `currentUser`；改登录态只有 `saveSession` / `clearSession` 两个入口。
 * - 401 的唯一出口是 `notifyUnauthorized()`：先清会话，再回调宿主（main.ts 注册）把页面送回登录页。
 *   本文件不 import router / api，避免 api → auth → router → view → api 的循环依赖。
 */
import { computed, ref } from 'vue'

export interface AuthUser {
  id: number
  username: string
  /** 后端 model.User 的字段名，保持原样透传 */
  real_name: string
  role: string
}

export interface LoginResult {
  token: string
  token_type: string
  expires_in: number
  user: AuthUser
}

const TOKEN_KEY = 'ceoc.token'
const USER_KEY = 'ceoc.user'
const USERNAME_KEY = 'ceoc.username'

const storage = typeof localStorage === 'undefined' ? null : localStorage

function readUser(): AuthUser | null {
  try {
    const raw = storage?.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as AuthUser) : null
  } catch {
    return null
  }
}

export const authToken = ref(storage?.getItem(TOKEN_KEY) || '')
export const currentUser = ref<AuthUser | null>(readUser())

export const isAuthed = computed(() => Boolean(authToken.value))
export const isAdmin = computed(() => currentUser.value?.role === 'admin')

export const displayName = computed(
  () => currentUser.value?.real_name?.trim() || currentUser.value?.username || '未登录',
)

export const roleLabel = computed(() => {
  const role = currentUser.value?.role
  if (role === 'admin') return '管理员'
  if (role === 'operator') return '运维员'
  return '只读'
})

/** 头像只放一个字：优先真名首字，没有真名用账号首字 */
export const avatarText = computed(() => (displayName.value.slice(0, 1) || '?').toUpperCase())

/** 上次登录成功的账号，用来预填登录表单（不是凭据，只是账号名） */
export function lastUsername(): string {
  return storage?.getItem(USERNAME_KEY) || ''
}

export function saveSession(result: LoginResult): void {
  storage?.setItem(TOKEN_KEY, result.token)
  storage?.setItem(USER_KEY, JSON.stringify(result.user))
  storage?.setItem(USERNAME_KEY, result.user.username)
  authToken.value = result.token
  currentUser.value = result.user
}

export function clearSession(): void {
  storage?.removeItem(TOKEN_KEY)
  storage?.removeItem(USER_KEY)
  authToken.value = ''
  currentUser.value = null
}

/* ---------- 401 出口 ---------- */

let unauthorizedHandler: (() => void) | null = null
/** 并发请求同时 401 时只跳一次，否则会叠出多条跳转与 toast */
let tripping = false

export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler
}

export function notifyUnauthorized(): void {
  if (tripping) return
  tripping = true
  clearSession()
  unauthorizedHandler?.()
  window.setTimeout(() => {
    tripping = false
  }, 400)
}
