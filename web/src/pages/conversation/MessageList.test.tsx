import { screen, waitFor } from '@testing-library/react'
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
  it('带消息定位链接进入已加载的对话时聚焦对应原文', async () => {
    const scroll = vi.fn()
    const previous = HTMLElement.prototype.scrollIntoView
    HTMLElement.prototype.scrollIntoView = scroll
    try {
      renderWithProviders(
        <MessageList
          detail={{
            ...detail,
            messages: [
              {
                message_id: 'm1',
                run_id: 'r1',
                seq: 12,
                client_message_id: null,
                role: 'assistant',
                content: '每周需要到岗四天',
                attachments_json: [],
                steps_json: [],
                created_at: '',
              },
            ],
          }}
          live={null}
          title={() => ''}
          decide={vi.fn()}
          refresh={vi.fn()}
          localHints={[]}
        />,
        { initialEntries: ['/conversations/c1#message-12'] },
      )

      const anchor = screen.getByText('每周需要到岗四天').closest('#message-12')
      await waitFor(() => expect(anchor).toHaveFocus())
      expect(scroll).toHaveBeenCalled()
    } finally {
      HTMLElement.prototype.scrollIntoView = previous
    }
  })

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
