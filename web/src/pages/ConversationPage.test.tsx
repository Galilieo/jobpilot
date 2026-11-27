import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
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

  it('附件超过总长度上限时显示后端的说明，附件和文字都留在输入框', async () => {
    const user = userEvent.setup()
    stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, DETAIL),
      'GET /api/jobs': () => jsonResponse(200, []),
      'POST /api/conversations/c1/messages': () =>
        jsonResponse(422, {
          detail: [
            { loc: ['body'], msg: 'Value error, 消息正文和附件的总长度不能超过 20833 字符' },
          ],
        }),
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: '添加附件' }))
    fireEvent.change(await screen.findByPlaceholderText('附件文字'), {
      target: { value: '很长的 JD' },
    })
    await user.click(screen.getByRole('button', { name: '加入附件' }))
    await typeAndSend('看看这个')

    expect(await screen.findByText(/总长度不能超过 20833 字符/)).toBeInTheDocument()
    expect(screen.getByText('附件 1')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByPlaceholderText(/发 JD/)).toHaveValue('看看这个'))
  })

  it('解除当前岗位后在对话里显示一行提示', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, { ...DETAIL, current_job_id: 'j1' }),
      'GET /api/jobs': () => jsonResponse(200, [{ job_id: 'j1', title: '后端实习生' }]),
      'GET /api/jobs/j1/matches': () => jsonResponse(200, []),
      'GET /api/jobs/j1': () => jsonResponse(404, {}),
      'GET /api/tailored-resumes': () => jsonResponse(200, []),
      'PUT /api/conversations/c1': () => jsonResponse(200, { ...DETAIL, current_job_id: null }),
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: /后端实习生/ }))
    await user.click(await screen.findByText('解除关联'))

    expect(await screen.findByText('已解除当前岗位')).toBeInTheDocument()
    expect(calls.find((c) => c.route === 'PUT /api/conversations/c1')?.body).toEqual({
      current_job_id: null,
    })
  })

  it('从新对话页带来的消息在开发模式重挂载后仍能读完流，不会卡在进行中', async () => {
    const pendingMessage = { clientMessageId: crypto.randomUUID(), content: 'JD', attachments: [] }
    const calls = stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, DETAIL),
      'GET /api/jobs': () => jsonResponse(200, []),
      'POST /api/conversations/c1/messages': () =>
        sseResponse([['done', { run_id: 'r1', status: 'succeeded', stop_reason: null }]]),
    })
    renderWithProviders(
      <StrictMode>
        <Routes>
          <Route path="/conversations/:conversationId" element={<ConversationPage />} />
        </Routes>
      </StrictMode>,
      { initialEntries: [{ pathname: '/conversations/c1', state: { pendingMessage } }] },
    )

    await waitFor(() => expect(screen.getByPlaceholderText(/发 JD/)).toBeInTheDocument())
    const sent = calls.filter((c) => c.route === 'POST /api/conversations/c1/messages')
    expect(
      new Set(sent.map((c) => (c.body as { client_message_id: string }).client_message_id)),
    ).toEqual(new Set([pendingMessage.clientMessageId]))
  })
})

describe('新对话页', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('入口建议只填进输入框不发送，页面展示三条保证', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({
      'GET /api/conversations': () => jsonResponse(200, []),
    })
    renderWithProviders(
      <Routes>
        <Route path="/conversations/new" element={<ConversationPage />} />
      </Routes>,
      { initialEntries: ['/conversations/new'] },
    )

    expect(screen.getByText('只用你确认过的经历')).toBeInTheDocument()
    expect(screen.getByText('改动前先问你')).toBeInTheDocument()
    expect(screen.getByText('每条都能查到依据')).toBeInTheDocument()

    const starter = screen.getByRole('button', { name: /发 JD 或截图/ })
    await user.click(starter)

    const input = screen.getByRole('textbox')
    expect((input as HTMLTextAreaElement).value).toContain('JD')
    expect(calls.some((c) => c.route === 'POST /api/conversations')).toBe(false)
  })
})

const USAGE = { used_tokens: 25_600, window_tokens: 128_000, compact_at_tokens: 94_000 }

describe('输入框右下角的上下文用量与压缩弹窗', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function openCompactModal(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: /上下文已用 20%/ }))
  }

  it('空重点时省略 focus 提交，202 focus_applied=true 只提示正在压缩较早的对话', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, DETAIL),
      'GET /api/jobs': () => jsonResponse(200, []),
      'GET /api/conversations/c1/context': () => jsonResponse(200, USAGE),
      'POST /api/conversations/c1/compact': () =>
        jsonResponse(202, { run_id: 'cr1', status: 'running', focus_applied: true }),
    })
    renderPage()

    await openCompactModal(user)
    const focusInput = await screen.findByPlaceholderText(/优先保留/)
    await user.type(focusInput, '   ')
    await user.click(screen.getByRole('button', { name: /开始压缩/ }))

    await waitFor(() => {
      const compactCall = calls.find((c) => c.route === 'POST /api/conversations/c1/compact')
      expect(compactCall?.body).toEqual({})
    })
    expect(await screen.findByText('正在压缩较早的对话')).toBeInTheDocument()
    expect(screen.queryByText(/这次的重点没有用上/)).not.toBeInTheDocument()
  })

  it('202 focus_applied=false、200 not_needed 与 409 分别给出对应提示', async () => {
    const user = userEvent.setup()
    let mode: 'busy_compact' | 'not_needed' | 'conflict' = 'busy_compact'
    const calls = stubFetch({
      'GET /api/conversations/c1': () => jsonResponse(200, DETAIL),
      'GET /api/jobs': () => jsonResponse(200, []),
      'GET /api/conversations/c1/context': () => jsonResponse(200, USAGE),
      'POST /api/conversations/c1/compact': () => {
        if (mode === 'busy_compact') {
          return jsonResponse(202, { run_id: 'cr1', status: 'running', focus_applied: false })
        }
        if (mode === 'not_needed') {
          return jsonResponse(200, { run_id: null, status: 'not_needed', focus_applied: false })
        }
        return jsonResponse(409, { detail: '正在运行中' })
      },
    })
    renderPage()

    await openCompactModal(user)
    await user.type(await screen.findByPlaceholderText(/优先保留/), ' 保留技术细节 ')
    await user.click(screen.getByRole('button', { name: /开始压缩/ }))

    await waitFor(() => {
      const compactCall = calls.find((c) => c.route === 'POST /api/conversations/c1/compact')
      expect(compactCall?.body).toEqual({ focus: '保留技术细节' })
    })
    expect(await screen.findByText(/已经有一次压缩在进行，这次的重点没有用上/)).toBeInTheDocument()

    mode = 'not_needed'
    await openCompactModal(user)
    await user.click(screen.getByRole('button', { name: /开始压缩/ }))
    expect(await screen.findByText('对话还不长，不需要压缩')).toBeInTheDocument()

    mode = 'conflict'
    await openCompactModal(user)
    await user.click(screen.getByRole('button', { name: /开始压缩/ }))
    expect(await screen.findByText('对话正在进行，等这一轮结束再压缩')).toBeInTheDocument()
  })
})
