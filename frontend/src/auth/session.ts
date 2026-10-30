/** 管理员会话状态：登录、恢复身份、改密和退出。 */

import { reactive } from 'vue'

import { request } from '../api/client'
import { clearToken, getToken, setToken } from '../api/token'
import type { AdminProfile } from '../api/types'

type AuthState = {
  admin: AdminProfile | null
}

export const authState = reactive<AuthState>({ admin: null })

export function hasToken(): boolean {
  return Boolean(getToken())
}

export async function login(username: string, password: string): Promise<AdminProfile> {
  const result = await request<{ access_token: string; user: AdminProfile }>('/api/v1/auth/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  setToken(result.access_token)
  authState.admin = result.user
  return result.user
}

export async function loadProfile(): Promise<AdminProfile> {
  const profile = await request<AdminProfile>('/api/v1/auth/admin/me')
  authState.admin = profile
  return profile
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  await request<void>('/api/v1/auth/admin/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  })
  if (authState.admin) {
    authState.admin = { ...authState.admin, is_first_login: false }
  }
}

export function logout(): void {
  clearToken()
  authState.admin = null
}
