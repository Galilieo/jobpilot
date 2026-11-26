// 新建对话并带着第一条待发消息跳转过去，供新对话页与工作台复用。
import { useQueryClient } from '@tanstack/react-query'
import { App } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { conversations } from '../../api/endpoints'
import { CONVERSATIONS_QUERY } from '../../queryKeys'
import { titleFrom } from './naming'
import type { LiveInput } from './runControl'

export interface PendingConversationState {
  pendingMessage?: LiveInput
}

export function useStartConversation() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)

  async function start(input: LiveInput): Promise<void> {
    setCreating(true)
    try {
      const created = await conversations.create(titleFrom(input))
      void queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY })
      // 交给对话页去发：发消息用的 ID 跟着走，刷新后重发也只会接上同一轮。
      void navigate(`/conversations/${created.conversation_id}`, {
        state: { pendingMessage: input } satisfies PendingConversationState,
      })
    } catch (error) {
      setCreating(false)
      void message.error(error instanceof Error ? error.message : '没能新建对话')
    }
  }

  return { start, creating }
}
