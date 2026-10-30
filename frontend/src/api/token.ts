/** 管理员 Bearer Token 的本地存储；刷新页面后用于恢复会话。 */

export const TOKEN_STORAGE_KEY = 'sagtrag_admin_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || ''
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY)
}
