import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, stubFetch } from '../test/fetchMock'
import { renderWithProviders } from '../test/render'
import { RecentConversations } from './RecentConversations'

const LIST = [
  {
    conversation_id: 'c-job',
    title: '后端实习',
    current_job_id: 'j1',
    created_at: '2026-11-26T00:00:00Z',
    updated_at: '2026-11-26T00:00:00Z',
  },
  {
    conversation_id: 'c-plain',
    title: '随便问问',
    current_job_id: null,
    created_at: '2026-11-26T00:00:00Z',
    updated_at: '2026-11-26T00:00:00Z',
  },
]

describe('最近对话', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('岗位对话和普通对话用不同图标', async () => {
    stubFetch({ 'GET /api/conversations': () => jsonResponse(200, LIST) })
    renderWithProviders(<RecentConversations />)

    expect(await screen.findByText('后端实习')).toBeInTheDocument()
    expect(screen.getByLabelText('岗位对话')).toBeInTheDocument()
    expect(screen.getByLabelText('普通对话')).toBeInTheDocument()
  })

  it('删除运行中的对话时显示后端给的原因', async () => {
    const user = userEvent.setup()
    stubFetch({
      'GET /api/conversations': () => jsonResponse(200, LIST),
      'DELETE /api/conversations/c-job': () =>
        jsonResponse(409, { detail: '对话正在运行，请稍后删除' }),
    })
    const { container } = renderWithProviders(<RecentConversations />)
    await screen.findByText('后端实习')

    const menuButtons = container.querySelectorAll('.ant-conversations-menu-icon')
    await user.click(menuButtons[0] as HTMLElement)
    await user.click(await screen.findByText('删除'))
    await user.click(await screen.findByRole('button', { name: '删除对话' }))

    await waitFor(() => expect(screen.getByText('对话正在运行，请稍后删除')).toBeInTheDocument())
  })
})
