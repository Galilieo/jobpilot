// 一段对话的运行控制：读详情（运行没结束时轮询）、发消息、提交确认决定，并把 SSE 合成进行中的一轮。
// 卡片和运行状态以详情为准；流只用来在详情刷新前先把进度显示出来。
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { conversations } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import { readAgentStream } from '../../api/sse'
import type { AgentDecision, ConversationDetail } from '../../api/types'
import {
  CONVERSATIONS_QUERY,
  JOBS_QUERY,
  OVERVIEW_QUERY,
  TAILORED_RESUMES_QUERY,
  conversationQuery,
} from '../../queryKeys'
import {
  applyEvent,
  emptyTurn,
  pollDelay,
  withNetworkRetry,
  type LiveInput,
  type LiveTurn,
} from './runControl'

// 一轮可能新建岗位、匹配报告和定制简历，侧栏、面板和工作台都要跟着更新（前缀匹配）。
const AFFECTED_QUERIES = [
  CONVERSATIONS_QUERY,
  JOBS_QUERY,
  OVERVIEW_QUERY,
  TAILORED_RESUMES_QUERY,
  ['tailor-run'],
  ['tailor-resume'],
  ['match-report'],
]

export type SendOutcome =
  | { saved: true }
  // 消息没有保存：把输入还给用户，并说明原因。
  | { saved: false; reason: string }

export function useConversationRun(conversationId: string) {
  const queryClient = useQueryClient()
  const [live, setLive] = useState<LiveTurn | null>(null)
  const polls = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const streaming = live !== null

  useEffect(() => () => controller.current?.abort(), [])

  const detail = useQuery({
    queryKey: conversationQuery(conversationId),
    queryFn: ({ signal }) => conversations.get(conversationId, signal),
    refetchOnWindowFocus: !streaming,
    refetchInterval: (query) => {
      if (streaming) return false
      const delay = pollDelay(query.state.data, polls.current)
      polls.current = delay === false ? 0 : polls.current + 1
      return delay
    },
  })

  async function refresh(): Promise<ConversationDetail | undefined> {
    for (const queryKey of AFFECTED_QUERIES) void queryClient.invalidateQueries({ queryKey })
    polls.current = 0
    const { data } = await detail.refetch()
    return data
  }

  /** 读完一个 SSE 响应；流断了也照样重读详情，由轮询接上后台还在跑的运行。 */
  async function follow(response: Response, signal: AbortSignal): Promise<void> {
    try {
      await readAgentStream(response, (item) => {
        if (!signal.aborted) setLive((turn) => turn && applyEvent(turn, item))
      })
    } catch {
      // 断线：后端运行照常完成，下面重读详情即可。
    }
  }

  async function stream(
    open: (signal: AbortSignal) => Promise<Response>,
    turn: LiveTurn,
  ): Promise<void> {
    controller.current?.abort()
    const current = new AbortController()
    controller.current = current
    setLive(turn)
    try {
      const response = await withNetworkRetry(() => open(current.signal))
      await follow(response, current.signal)
    } finally {
      if (!current.signal.aborted) {
        await refresh()
        setLive(null)
      }
    }
  }

  async function send(input: LiveInput): Promise<SendOutcome> {
    try {
      await stream(
        (signal) =>
          conversations.sendMessage(
            conversationId,
            {
              client_message_id: input.clientMessageId,
              content: input.content,
              attachments: input.attachments,
            },
            signal,
          ),
        emptyTurn(input),
      )
      return { saved: true }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return { saved: true }
      // 重试用尽后，请求也可能其实已经到了服务端：以详情里有没有这条消息为准。
      const latest = queryClient.getQueryData<ConversationDetail>(conversationQuery(conversationId))
      const saved = latest?.messages.some(
        (message) => message.client_message_id === input.clientMessageId,
      )
      if (saved) return { saved: true }
      return {
        saved: false,
        reason: error instanceof ApiError ? error.message : '消息没有发出去，请重试。',
      }
    }
  }

  /** 提交确认卡的决定。返回失败原因，成功时返回 null。 */
  async function decide(actionId: string, decision: AgentDecision): Promise<string | null> {
    try {
      await stream(
        (signal) => conversations.decide(conversationId, actionId, decision, signal),
        emptyTurn(),
      )
      return null
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return null
      return error instanceof ApiError ? error.message : '决定没有提交成功，请重试。'
    }
  }

  return { detail, live, streaming, send, decide, refresh }
}
