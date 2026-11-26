// 对话运行的前端控制规则：把 SSE 事件合进「进行中的一轮」，决定何时轮询详情、何时重试。
// 这里只放纯函数和常量；网络请求与 React 状态在 useConversationRun 里。
import { ApiError, NETWORK_ERROR_STATUS } from '../../api/http'
import type {
  AgentActionCard,
  AgentStreamEvent,
  AgentTextAttachment,
  ConversationDetail,
} from '../../api/types'

// 断线或刷新后，运行没结束就每隔这么久重读一次详情。
export const POLL_MS = 2000
// 后端一轮最长 180 秒活动时间，再留出恢复的余量；超过就不再自动轮询，改由用户手动刷新。
export const MAX_POLLS = 120
// 拿不到响应时用同一个 client_message_id 重试的次数和间隔，服务端按这个 ID 去重。
export const NETWORK_RETRIES = 2
export const RETRY_DELAY_MS = 1000

export interface LiveStep {
  id: string
  tool: string
  status: 'loading' | 'success' | 'error'
  result: Record<string, unknown> | null
}

export interface LiveInput {
  clientMessageId: string
  content: string
  attachments: AgentTextAttachment[]
}

/** 正在流式进行的一轮：发出的消息（未进详情前先显示）、模型文字、工具步骤、新出的卡。 */
export interface LiveTurn {
  input: LiveInput | null
  text: string
  steps: LiveStep[]
  card: AgentActionCard | null
  error: string | null
}

export function emptyTurn(input: LiveInput | null = null): LiveTurn {
  return { input, text: '', steps: [], card: null, error: null }
}

export function applyEvent(turn: LiveTurn, item: AgentStreamEvent): LiveTurn {
  switch (item.event) {
    case 'text':
      return { ...turn, text: turn.text + item.data.text }
    case 'tool_start':
      // 调工具前的那段文字不会作为回复保存，跟详情保持一致，开始调工具就清掉。
      return {
        ...turn,
        text: '',
        steps: [
          ...turn.steps,
          { id: item.data.tool_call_id, tool: item.data.tool, status: 'loading', result: null },
        ],
      }
    case 'tool_end': {
      const { tool, tool_call_id: id = `${tool}-${turn.steps.length}`, ...result } = item.data
      const step: LiveStep = { id, tool, status: result.error ? 'error' : 'success', result }
      const known = turn.steps.some((existing) => existing.id === id)
      return {
        ...turn,
        steps: known
          ? turn.steps.map((existing) => (existing.id === id ? step : existing))
          : [...turn.steps, step], // 重放的流只有 tool_end
      }
    }
    case 'confirm':
      return { ...turn, card: item.data }
    case 'error':
      return { ...turn, error: item.data.message ?? item.data.error ?? '这一轮没有完成' }
    case 'done':
      return turn
  }
}

/** 运行还在后台进行（或决定已保存、等待恢复）时返回轮询间隔，否则不轮询。 */
export function pollDelay(detail: ConversationDetail | undefined, polls: number): number | false {
  if (!detail || polls >= MAX_POLLS) return false
  const active =
    detail.run?.status === 'running' ||
    detail.confirmation_status === 'decision_saved' ||
    detail.confirmation_status === 'recovering'
  return active ? POLL_MS : false
}

function isNetworkError(error: unknown): boolean {
  return error instanceof ApiError && error.status === NETWORK_ERROR_STATUS
}

/** 只在拿不到响应时重试；调用方保证每次请求带同一个幂等 ID。 */
export async function withNetworkRetry<T>(
  attempt: () => Promise<T>,
  { retries = NETWORK_RETRIES, delayMs = RETRY_DELAY_MS } = {},
): Promise<T> {
  for (let tried = 0; ; tried += 1) {
    try {
      return await attempt()
    } catch (error) {
      if (!isNetworkError(error) || tried >= retries) throw error
      await new Promise((resolve) => setTimeout(resolve, delayMs))
    }
  }
}
