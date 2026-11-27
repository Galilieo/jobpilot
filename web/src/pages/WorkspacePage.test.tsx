import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, stubFetch } from '../test/fetchMock'
import { renderWithProviders } from '../test/render'
import { WorkspacePage } from './WorkspacePage'

const OVERVIEW_DATA = {
  facts: { published_count: 10, version: 1, pending_count: 0 },
  grounding: { total: 4, passed: 4, fallback: 0, pending: 0, resume_count: 1 },
  board: {
    unmatched: [
      {
        job_id: 'j1',
        title: '检索算法实习',
        company: '样例数据',
        salary: null,
        location: '北京',
        onsite_days_per_week: '每周 5 天',
        education: null,
        stage: 'saved',
        match_summary: null,
        updated_at: '2026-11-25T10:00:00Z',
        resume_stale: false,
      },
    ],
    matched: [
      {
        job_id: 'j2',
        title: '大模型应用开发实习',
        company: '青梧数智',
        salary: '250-300 元/天',
        location: '上海',
        onsite_days_per_week: '每周 4 天',
        education: '本科',
        stage: 'saved',
        match_summary: { hard_covered: 4, hard_total: 9, expression_gap: 1 },
        updated_at: '2026-11-25T10:00:00Z',
        resume_stale: false,
      },
    ],
    tailored: [],
    applied: [],
  },
}

const MEMORIES_DATA = [
  { key: 'target_direction', label: '目标方向', value: null, revision: 0, deleted: false },
  { key: 'cities', label: '城市', value: ['上海', '杭州'], revision: 1, deleted: false },
  { key: 'excluded_types', label: '排除类型', value: null, revision: 0, deleted: false },
  { key: 'job_notes', label: '其他求职偏好', value: null, revision: 0, deleted: false },
  { key: 'tone', label: '语气', value: '平实', revision: 1, deleted: false },
  { key: 'bullet_length', label: '每条长度', value: null, revision: 0, deleted: false },
  { key: 'tech_terms', label: '技术名写法', value: null, revision: 0, deleted: false },
]

describe('WorkspacePage 工作台布局、顶部输入、记忆分栏与看板', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('展示横向三栏（含记住的偏好）、分开的元信息小块与空列下一步提示', async () => {
    stubFetch({
      'GET /api/me': () =>
        jsonResponse(200, {
          user_id: 'u1',
          email: 'tester@example.invalid',
          deletion_requested_at: null,
          deletion_deadline: null,
        }),
      'GET /api/overview': () => jsonResponse(200, OVERVIEW_DATA),
      'GET /api/memories': () => jsonResponse(200, MEMORIES_DATA),
    })

    const { container } = renderWithProviders(
      <Routes>
        <Route path="/workspace" element={<WorkspacePage />} />
      </Routes>,
      { initialEntries: ['/workspace'] },
    )

    expect(await screen.findByText('250-300 元/天')).toBeInTheDocument()
    // 元信息分开排成小块，不再用中点串
    expect(screen.queryByText('青梧数智 · 上海 · 每周 4 天 · 本科')).not.toBeInTheDocument()
    expect(screen.getByText('青梧数智')).toBeInTheDocument()
    expect(screen.getByText('上海')).toBeInTheDocument()
    expect(screen.getByText('每周 4 天')).toBeInTheDocument()
    expect(screen.getByText('本科')).toBeInTheDocument()
    expect(screen.getByText('硬性 4/9 · 表达缺口 1')).toBeInTheDocument()
    expect(screen.getByText('样例数据')).toBeInTheDocument()
    expect(screen.getByText('还没匹配')).toBeInTheDocument()

    // 横向三栏读取记住的偏好，链接指向我的 -> 记忆
    expect(container.querySelector('.workspace-ledger')).toBeInTheDocument()
    expect(screen.getByText(/上海、杭州/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '管理' })).toHaveAttribute('href', '/me?tab=memories')

    // 空列为虚线框并给出下一步
    expect(container.querySelectorAll('.board-column.is-empty')).toHaveLength(2)
    expect(screen.getByText('生成定制简历后会出现在这里。')).toBeInTheDocument()
    expect(screen.getByText('记录投递后会出现在这里。')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看全部投递' })).toHaveAttribute(
      'href',
      '/applications',
    )
  })

  it('顶部输入框发出后新建对话并跳过去', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({
      'GET /api/me': () =>
        jsonResponse(200, {
          user_id: 'u1',
          email: 'tester@example.invalid',
          deletion_requested_at: null,
          deletion_deadline: null,
        }),
      'GET /api/overview': () => jsonResponse(200, OVERVIEW_DATA),
      'GET /api/memories': () => jsonResponse(200, MEMORIES_DATA),
      'POST /api/conversations': () =>
        jsonResponse(201, {
          conversation_id: 'c-new',
          title: '帮我看看这个后端岗位',
          current_job_id: null,
          created_at: '2026-11-26T00:00:00Z',
          updated_at: '2026-11-26T00:00:00Z',
        }),
    })

    renderWithProviders(
      <Routes>
        <Route path="/workspace" element={<WorkspacePage />} />
        <Route path="/conversations/:conversationId" element={<div>已跳转到新对话页</div>} />
      </Routes>,
      { initialEntries: ['/workspace'] },
    )

    const input = await screen.findByPlaceholderText(/粘贴一段 JD/)
    await user.type(input, '帮我看看这个后端岗位')
    await user.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByText('已跳转到新对话页')).toBeInTheDocument())
    expect(calls.some((c) => c.route === 'POST /api/conversations')).toBe(true)
  })
})
