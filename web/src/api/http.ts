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

// 没拿到响应（断网、服务器没起来）时 ApiError 的 status。
export const NETWORK_ERROR_STATUS = 0

const STATUS_MESSAGES: Record<number, string> = {
  401: '登录已过期，请重新登录。',
  404: '没有找到这项内容，可能已被删除。',
  409: '内容已在别处更新，请刷新后再试。',
  410: '这条记录已经删除，如需重新记录请重新提交',
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
    // 模型级校验（model_validator）的说明是写给用户看的，字段级校验才用通用提示。
    if (first && typeof first === 'object' && 'msg' in first && typeof first.msg === 'string') {
      const custom = /^Value error, (.+)$/.exec(first.msg)
      if (custom) return custom[1]
    }
    return '填写的内容不符合要求，请检查后再试。'
  }
  return STATUS_MESSAGES[status] ?? '出了点问题，请稍后重试。'
}

async function send(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(path, { ...init, credentials: 'same-origin' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(NETWORK_ERROR_STATUS, '网络连接失败，请检查网络后重试。', null)
  }
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

  const response = await send(path, {
    method: options.method ?? (body ? 'POST' : 'GET'),
    headers,
    body,
    signal: options.signal,
  })

  if (response.status === 204) return undefined as T
  if (!response.ok) throw await errorFromResponse(response)
  return (await response.json().catch(() => null)) as T
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const payload: unknown = await response.json().catch(() => null)
  const detail =
    payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload
  return new ApiError(response.status, messageOf(response.status, detail), detail)
}

/** 发起返回 SSE 的 POST。只在拿到响应前出错时抛网络错误，响应体交给调用方去读。 */
export async function postForStream(
  path: string,
  json: unknown,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await send(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(json),
    signal,
  })
  if (!response.ok) throw await errorFromResponse(response)
  return response
}
