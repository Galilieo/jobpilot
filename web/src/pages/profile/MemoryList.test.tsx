import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MemoryItem, WritingPreference } from '../../api/types'
import { jsonResponse, stubFetch } from '../../test/fetchMock'
import { renderWithProviders } from '../../test/render'
import { MemoryList } from './MemoryList'

const defaultWriting: WritingPreference = {
  values: { tone: null, bullet_length: null, tech_terms: null },
  revision: 0,
  options: { tone: ['平实', '简洁有力'], bullet_length: ['尽量一行'], tech_terms: ['英文原名'] },
  labels: { tone: '语气', bullet_length: '每条长度', tech_terms: '技术名写法' },
}

const memory: MemoryItem = {
  memory_id: 'm1',
  category: 'job_pref',
  content: '不投外包公司',
  revision: 2,
  source: { kind: 'conversation', conversation_id: 'c1', message_id: 'msg1', quote: '我不投外包' },
  conversation_title: '讨论岗位',
  created_at: '2026-11-28T06:00:00Z',
  updated_at: '2026-11-28T06:00:00Z',
}

describe('记忆管理', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('显示空状态并允许手动添加', async () => {
    const user = userEvent.setup()
    let entries: MemoryItem[] = []
    const calls = stubFetch({
      'GET /api/writing-preference': () => jsonResponse(200, defaultWriting),
      'GET /api/memories': () =>
        jsonResponse(200, { entries, used_chars: entries.length * 6, budget_chars: 1500 }),
      'POST /api/memories': () => {
        const added = { ...memory, source: { kind: 'manual' as const } }
        entries = [added]
        return jsonResponse(200, { status: 'saved', entry: added })
      },
    })
    renderWithProviders(<MemoryList />)
    expect(await screen.findByText(/在对话里说出你的偏好/)).toBeInTheDocument()
    expect(screen.getByText(/对话从下一轮起生效/)).toBeInTheDocument()
    expect(screen.getByText('页面改动从下一轮起生效。')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '手动添加' }))
    await user.type(screen.getByRole('textbox', { name: '记忆内容' }), memory.content)
    await user.click(screen.getByRole('button', { name: /保\s?存/ }))

    expect(await screen.findByText(memory.content)).toBeInTheDocument()
    expect(screen.getByText('已用 6 / 1500 字')).toBeInTheDocument()
    expect(calls.find((call) => call.route === 'POST /api/memories')?.body).toEqual({
      category: 'job_pref',
      content: memory.content,
    })
  })

  it('使用条目修订号修改后删除，并显示来源和用量提醒', async () => {
    const user = userEvent.setup()
    let entries = [memory]
    const calls = stubFetch({
      'GET /api/writing-preference': () => jsonResponse(200, defaultWriting),
      'GET /api/memories': () =>
        jsonResponse(200, { entries, used_chars: entries.length * 1201, budget_chars: 1500 }),
      'PUT /api/memories/m1': () => {
        const updated = { ...memory, content: '优先自研', revision: 3 }
        entries = [updated]
        return jsonResponse(200, { status: 'saved', entry: updated })
      },
      'DELETE /api/memories/m1': () => {
        entries = []
        return jsonResponse(204, null)
      },
    })
    renderWithProviders(<MemoryList />)
    expect(await screen.findByRole('link', { name: '来自《讨论岗位》' })).toHaveAttribute(
      'href',
      '/conversations/c1',
    )
    expect(screen.getByText(/记忆快满了/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /编\s?辑/ }))
    const input = screen.getByRole('textbox', { name: '记忆内容' })
    await user.clear(input)
    await user.type(input, '优先自研')
    await user.click(screen.getByRole('button', { name: /保\s?存/ }))
    expect(await screen.findByText('优先自研')).toBeInTheDocument()
    expect(calls.find((call) => call.route === 'PUT /api/memories/m1')?.body).toEqual({
      category: 'job_pref',
      content: '优先自研',
      expected_revision: 2,
    })
    await user.click(screen.getByRole('button', { name: /删\s?除/ }))
    await user.click(screen.getAllByRole('button', { name: /删\s?除/ }).at(-1)!)
    expect(await screen.findByText('已用 0 / 1500 字')).toBeInTheDocument()
    expect(calls.find((call) => call.route === 'DELETE /api/memories/m1')?.body).toEqual({
      expected_revision: 3,
    })
  })

  it('点选时保存单项写作偏好，默认选项提交 null', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({
      'GET /api/writing-preference': () => jsonResponse(200, defaultWriting),
      'GET /api/memories': () =>
        jsonResponse(200, { entries: [], used_chars: 0, budget_chars: 1500 }),
      'PUT /api/writing-preference': (body, index) =>
        jsonResponse(200, {
          ...defaultWriting,
          revision: index + 1,
          values: { ...defaultWriting.values, tone: (body as { tone: string | null }).tone },
        }),
    })
    renderWithProviders(<MemoryList />)
    await user.click((await screen.findByRole('radio', { name: '平实' })).closest('label')!)
    await waitFor(() => expect(screen.getByRole('radio', { name: '平实' })).toBeChecked())
    await user.click(screen.getAllByRole('radio', { name: '默认' })[0].closest('label')!)
    await waitFor(() =>
      expect(
        calls
          .filter((call) => call.route === 'PUT /api/writing-preference')
          .map((call) => call.body),
      ).toEqual([
        { expected_revision: 0, tone: '平实' },
        { expected_revision: 1, tone: null },
      ]),
    )
  })
})
