import type { Identity } from './types'
import { shallowRef } from 'vue'

export const identity = shallowRef<Identity | null>(null)
export const sessionError = shallowRef('')
let restored = false
let unauthorizedHandler: (() => void) | undefined

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly requestId: string | null,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export function onUnauthorized(handler: () => void) {
  unauthorizedHandler = handler
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (!headers.has('Accept'))
    headers.set('Accept', 'application/json')
  if (options.body)
    headers.set('Content-Type', 'application/json')
  if (options.method && options.method !== 'GET' && identity.value)
    headers.set('X-CSRF-Token', identity.value.csrf_token)

  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, {
      ...options,
      headers,
      credentials: 'same-origin',
    })
  }
  catch (cause) {
    if (options.signal?.aborted)
      throw cause
    throw new ApiError('无法连接服务，请检查网络或后端服务。', 0, 'network_error', null)
  }

  if (response.status === 401) {
    const hadSession = identity.value !== null
    identity.value = null
    if (hadSession) {
      sessionError.value = '登录已失效，请重新登录。'
      unauthorizedHandler?.()
    }
  }

  if (response.status === 204)
    return undefined as T

  if (response.ok && headers.get('Accept') === 'image/*') {
    if (!response.headers.get('Content-Type')?.startsWith('image/'))
      throw new ApiError('二维码接口未返回有效图像。', response.status, 'invalid_image', response.headers.get('X-Request-ID'))
    return await response.blob() as T
  }

  let body: unknown
  try {
    body = await response.json()
  }
  catch {
    throw new ApiError('服务返回了无法识别的响应。', response.status, 'invalid_response', response.headers.get('X-Request-ID'))
  }

  if (!response.ok) {
    const error = body as { message?: string, code?: string, request_id?: string }
    throw new ApiError(
      response.status === 409
        ? `${error?.message || '状态或配置已发生变化。'} 请刷新后核对最新状态再操作。`
        : error?.message || `请求失败（HTTP ${response.status}）。`,
      response.status,
      error?.code || 'request_failed',
      error?.request_id || response.headers.get('X-Request-ID'),
    )
  }
  return body as T
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError)
    return error.requestId ? `${error.message} · 请求编号 ${error.requestId}` : error.message
  return '请求未完成，请重试。'
}

export async function restoreSession() {
  if (restored)
    return
  try {
    identity.value = await request<Identity>('/auth/me')
  }
  catch (error) {
    if (!(error instanceof ApiError && error.status === 401))
      sessionError.value = errorMessage(error)
  }
  finally {
    restored = true
  }
}

export async function login(username: string, password: string) {
  identity.value = await request<Identity>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  sessionError.value = ''
}

export async function logout() {
  await request<void>('/auth/logout', { method: 'POST' })
  identity.value = null
}
