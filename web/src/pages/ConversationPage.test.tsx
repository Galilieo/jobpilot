import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ConversationDetail } from '../api/types'
import { jsonResponse, sseResponse, stubFetch } from '../test/fetchMock'
import { renderWithProviders } from '../test/render'
import { ConversationPage } from './ConversationPage'

const DETAIL: ConversationDetail = {
  conversation_id: 'c1',
  title: '后端实习',
  current_job_id: null,
  created_at: '2026-11-26T00:00:00Z',
  updated_at: '2026-11-26T00:00:00Z',
  messages: [],
  run: null,
  pending_action: null,
  latest_decided_action: null,
  confirmation_status: null,
}

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/conversations/:conversationId" element={<ConversationPage />} />
    </Routes>,
    { initialEntries: ['/conversations/c1'] },
  )
}

async function typeAndSend(text: string) {
  const user = userEvent.setup()
  const input = await screen.findByPlaceholderText(/发 JD/)
  await user.type(input, text)
  await user.keyboard('{Enter}')
  return input
}

describe('对话页发消息', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('409 时告诉用户消息没有保存，并把文字还回输入框', async () => {
    stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, DETAIL),
      'GET /api/jobs': () => jsonResponse(200, []),
      'POST /api/conversations/c1/messages': () =>
        jsonResponse(409, {
          detail: { message: '这条消息没有保存', run_id: 'r0', pending_action: null },
        }),
    })
    renderPage()

    const input = await typeAndSend('帮我匹配一下')
    expect(await screen.findByText('这条消息没有保存')).toBeInTheDocument()
    await waitFor(() => expect(input).toHaveValue('帮我匹配一下'))
  })

  it('拿不到响应时用同一个 client_message_id 重试', async () => {
    const calls = stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, DETAIL),
      'GET /api/jobs': () => jsonResponse(200, []),
      'POST /api/conversations/c1/messages': (_, call) => {
        if (call === 0) throw new TypeError('Failed to fetch')
        return sseResponse([
          ['text', { text: '好的' }],
          ['done', { run_id: 'r1', status: 'succeeded', stop_reason: null }],
        ])
      },
    })
    renderPage()

    await typeAndSend('你好')
    await waitFor(
      () =>
        expect(calls.filter((c) => c.route === 'POST /api/conversations/c1/messages')).toHaveLength(
          2,
        ),
      { timeout: 3000 },
    )
    const ids = calls
      .filter((c) => c.route === 'POST /api/conversations/c1/messages')
      .map((c) => (c.body as { client_message_id: string }).client_message_id)
    expect(ids[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(ids[1]).toBe(ids[0])
  })
})
