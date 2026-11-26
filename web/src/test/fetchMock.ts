// 对话相关测试共用的假 fetch：按「方法 + 路径」给响应，记录每次请求的请求体。
import { vi } from 'vitest'

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export function sseResponse(events: [string, unknown][]): Response {
  const text = events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)
  return new Response(text.join(''), { headers: { 'Content-Type': 'text/event-stream' } })
}

type Handler = (body: unknown, call: number) => Response | Promise<Response>

export interface FetchCall {
  route: string
  body: unknown
}

/** routes 的键形如 `POST /api/conversations`；同一路由的处理函数收到第几次调用（从 0 开始）。 */
export function stubFetch(routes: Record<string, Handler>): FetchCall[] {
  const calls: FetchCall[] = []
  const counts = new Map<string, number>()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      const route = `${init?.method ?? 'GET'} ${url}`
      const body: unknown = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
      calls.push({ route, body })
      const handler = routes[route]
      if (!handler) return jsonResponse(404, { detail: `测试没有准备 ${route}` })
      const count = counts.get(route) ?? 0
      counts.set(route, count + 1)
      return handler(body, count)
    }),
  )
  return calls
}
