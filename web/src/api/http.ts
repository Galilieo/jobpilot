// 所有接口共用的请求函数：带 Cookie、JSON 或表单、幂等键，并把错误整理成给人看的一句话。
// 只负责 HTTP 契约；缓存和界面状态交给调用方的 React Query。

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  json?: unknown
  form?: FormData
  // 有费用的 POST 必填。重试同一次操作时传同一个键，服务端只执行一次。
  idempotencyKey?: string
  signal?: AbortSignal
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID()
}

const STATUS_MESSAGES: Record<number, string> = {
  401: '登录已过期，请重新登录。',
  404: '没有找到这项内容，可能已被删除。',
  409: '内容已在别处更新，请刷新后再试。',
  502: '模型服务暂时不可用，请稍后重试。',
}

function messageOf(status: number, detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String(detail.message)
  }
  if (Array.isArray(detail) && detail.length > 0) {
    // 422：FastAPI 的校验错误是对象列表；业务错误是文字列表。
    const first: unknown = detail[0]
    if (typeof first === 'string') return detail.join('；')
    return '填写的内容不符合要求，请检查后再试。'
  }
  return STATUS_MESSAGES[status] ?? '出了点问题，请稍后重试。'
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}
  let body: BodyInit | undefined
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.json)
  } else if (options.form) {
    body = options.form // 浏览器自动带上 multipart 边界
  }
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey

  let response: Response
  try {
    response = await fetch(path, {
      method: options.method ?? (body ? 'POST' : 'GET'),
      credentials: 'same-origin',
      headers,
      body,
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, '网络连接失败，请检查网络后重试。', null)
  }

  if (response.status === 204) return undefined as T
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload
    throw new ApiError(response.status, messageOf(response.status, detail), detail)
  }
  return payload as T
}
