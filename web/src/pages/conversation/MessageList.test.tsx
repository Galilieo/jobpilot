import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ConversationDetail } from '../../api/types'
import { renderWithProviders } from '../../test/render'
import { MessageList } from './MessageList'
import { emptyTurn } from './runControl'

const detail: ConversationDetail = {
  conversation_id: 'c1',
  title: '你好',
  current_job_id: null,
  created_at: '',
  updated_at: '',
  messages: [],
  run: null,
  pending_action: null,
  latest_decided_action: null,
  confirmation_status: null,
}

describe('消息列表', () => {
  // 新对话发出第一条消息后会跳到对话页，消息列表挂载时这一轮已经开始；
  // 此时 Bubble.List 的滚动容器还没就绪，直接滚动会抛错，整个页面变白。
  it('挂载时这一轮已经开始也能正常显示', () => {
    renderWithProviders(
      <MessageList
        detail={detail}
        live={emptyTurn({ clientMessageId: 'm1', content: '你好', attachments: [] })}
        title={() => ''}
        decide={vi.fn()}
        refresh={vi.fn()}
        localHints={[]}
      />,
    )

    expect(screen.getByText('你好')).toBeInTheDocument()
  })
})
