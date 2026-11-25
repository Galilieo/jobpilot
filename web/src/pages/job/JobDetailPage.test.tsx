import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { JobDetailPage } from './JobDetailPage'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const JOB_DETAIL = {
  job_id: 'job-1',
  stage: 'saved',
  note: null,
  raw_text: 'Python 后端实习生',
  parsed: {
    basic_fields: {
      company: '青梧数智',
      title: '大模型应用开发实习生',
      location: '上海',
      salary: '250-300 元/天',
      onsite_days_per_week: '每周 4 天',
      internship_duration: null,
      education: '本科',
      graduation_window: null,
      application_deadline: null,
    },
    requirements: [],
    duties: [],
    keywords: ['Python'],
  },
  overridden: [],
  overridden_basic_fields: [],
  injection_flags: [],
  created_at: '2026-11-25T10:00:00Z',
  updated_at: '2026-11-25T10:00:00Z',
}

describe('JobDetailPage 删除岗位二次确认', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('菜单保留归档岗位，点删除岗位弹出二次确认，取消不请求，确认后删除并跳回岗位库', async () => {
    const user = userEvent.setup()
    const calls: { method: string; url: string }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? 'GET'
        calls.push({ method, url })
        if (url === '/api/jobs/job-1' && method === 'GET') {
          return jsonResponse(200, JOB_DETAIL)
        }
        if (url === '/api/jobs/job-1' && method === 'DELETE') {
          return new Response(null, { status: 204 })
        }
        if (
          url === '/api/jobs/job-1/matches' ||
          url === '/api/jobs/job-1/tailor-runs' ||
          url === '/api/facts'
        ) {
          return jsonResponse(
            200,
            url === '/api/facts'
              ? {
                  fact_set: null,
                  facts: [],
                  criteria: [],
                  education: [],
                  draft: {
                    base_fact_set_id: null,
                    items: [],
                    criteria: [],
                    education: [],
                    revision: 0,
                  },
                }
              : [],
          )
        }
        return jsonResponse(404, { detail: 'unexpected' })
      }),
    )

    renderWithProviders(
      <Routes>
        <Route path="/jobs/:jobId" element={<JobDetailPage />} />
        <Route path="/jobs" element={<div>岗位库列表页</div>} />
      </Routes>,
      { initialEntries: ['/jobs/job-1'] },
    )

    expect(await screen.findByRole('heading', { name: '大模型应用开发实习生' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '更多岗位操作' }))
    expect(await screen.findByRole('menuitem', { name: '归档岗位' })).toBeInTheDocument()
    const deleteMenuItem = await screen.findByRole('menuitem', { name: '删除岗位' })
    await user.click(deleteMenuItem)

    expect(
      await screen.findByText(
        '会一起删除这个岗位的全部匹配报告、改写记录和定制简历，删除后无法恢复。',
      ),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '取 消' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)

    await user.click(screen.getByRole('button', { name: '更多岗位操作' }))
    await user.click(await screen.findByRole('menuitem', { name: '删除岗位' }))
    const confirmButtons = await screen.findAllByRole('button', { name: '删除岗位' })
    await user.click(confirmButtons[confirmButtons.length - 1])

    await waitFor(() =>
      expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/jobs/job-1')).toBe(true),
    )
    expect(await screen.findByText('岗位库列表页')).toBeInTheDocument()
  })
})
