import { ref } from 'vue'
import type { LoginResult, User } from '../types'
import { resetDesktopSession } from './desktop'

const TOKEN_KEY = 'powersystem_token'
const USER_KEY = 'powersystem_user'
const storage = typeof sessionStorage === 'undefined' ? null : sessionStorage

function readUser(): User | null {
  try { return JSON.parse(storage?.getItem(USER_KEY) || 'null') as User | null }
  catch { return null }
}

export const token = ref(storage?.getItem(TOKEN_KEY) || '')
export const currentUser = ref<User | null>(readUser())

export function saveSession(result: LoginResult): void {
  resetDesktopSession()
  storage?.setItem(TOKEN_KEY, result.token)
  storage?.setItem(USER_KEY, JSON.stringify(result.user))
  token.value = result.token
  currentUser.value = result.user
}

export function clearSession(): void {
  resetDesktopSession()
  storage?.removeItem(TOKEN_KEY)
  storage?.removeItem(USER_KEY)
  token.value = ''
  currentUser.value = null
}
