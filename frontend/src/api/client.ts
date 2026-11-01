/** 统一的 API 客户端：附加 Bearer Token、中文错误提示与失效会话回调。 */

import { getToken } from './token'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://127.0.0.1:8000'

/** 后端已知的英文错误码到中文提示的映射，避免把内部错误直接展示给管理员。 */
const DETAIL_MESSAGES: Record<string, string> = {
  'Invalid username or password': '用户名或密码不正确，请重试。',
  'Current password is incorrect': '当前密码不正确。',
  'New password must be different': '新密码不能与当前密码相同。',
  'Password change required': '请先完成首次登录密码修改。',
  'Authentication required': '请先登录。',
  'Invalid or expired session': '登录已失效，请重新登录。',
  'Administrator account is inactive': '账号已停用或不存在。',
  'Administrator username already exists': '该管理员账号名已被使用。',
  'Administrator not found': '管理员账号不存在。',
  'Cannot delete the current administrator': '不能删除当前登录账号。',
  'Cannot disable the current administrator': '不能停用当前登录账号。',
  'At least one active administrator is required': '必须保留至少一个启用状态的管理员。',
  'WeCom userid is already bound': '该企微 userid 已绑定其他员工。',
  'WeCom binding is already removed': '该员工尚未绑定企微。',
  'Document not found': '文档不存在或已被删除。',
  'User not found': '员工不存在。',
  'Invalid filename': '文件名不合法，请重命名后重试。',
  'Document could not be queued': '文档暂时无法入库，请稍后重试。',
}

const MAX_UPLOAD_MEGABYTES = 20

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

let unauthorizedHandler: (() => void) | null = null

/** 注册 401 处理（清除本地会话并跳转登录页）。 */
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

function mapDetail(detail: unknown): string | null {
  if (typeof detail !== 'string') {
    return Array.isArray(detail) ? '提交的数据不完整或格式不正确。' : null
  }
  return DETAIL_MESSAGES[detail] ?? null
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  } catch {
    throw new ApiError('无法连接服务器，请确认后端服务已启动。', 0)
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown } | null
    const detail = body?.detail
    const isLogin = path.endsWith('/auth/admin/login')
    if (response.status === 401 && !isLogin) {
      unauthorizedHandler?.()
      throw new ApiError('登录已失效，请重新登录。', 401)
    }
    const message = mapDetail(detail)
      ?? (typeof detail === 'string' ? detail : `请求失败（${response.status}）。`)
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) {
    return null as T
  }
  return (await response.json()) as T
}

export function validateUploadFile(file: File): string | null {
  const extension = file.name.toLowerCase().slice(file.name.lastIndexOf('.'))
  const allowed = ['.pdf', '.docx', '.txt', '.md', '.markdown']
  if (!allowed.includes(extension)) {
    return '仅支持 PDF、DOCX、TXT、Markdown 格式。'
  }
  if (file.size > MAX_UPLOAD_MEGABYTES * 1024 * 1024) {
    return `文件大小不能超过 ${MAX_UPLOAD_MEGABYTES} MB。`
  }
  if (file.size === 0) {
    return '不能上传空文件。'
  }
  return null
}
