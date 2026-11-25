import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../test/render'
import { WorkspacePage } from './WorkspacePage'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('WorkspacePage 看板卡片字段与匹配摘要', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('看板卡片展示薪资、公司·地点·到岗·学历与匹配摘要', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url === '/api/me') {
          return jsonResponse(200, {
            user_id: 'u1',
            email: 'tester@example.invalid',
            deletion_requested_at: null,
            deletion_deadline: null,
          })
        }
        if (url === '/api/overview') {
          return jsonResponse(200, {
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
          })
        }
        return jsonResponse(404, { detail: 'unexpected' })
      }),
    )

    renderWithProviders(<WorkspacePage />)

    expect(await screen.findByText('250-300 元/天')).toBeInTheDocument()
    expect(screen.getByText('青梧数智 · 上海 · 每周 4 天 · 本科')).toBeInTheDocument()
    expect(screen.getByText('硬性 4/9 · 表达缺口 1')).toBeInTheDocument()
    expect(screen.getByText('样例数据 · 北京 · 每周 5 天')).toBeInTheDocument()
    expect(screen.getByText('还没匹配')).toBeInTheDocument()
  })
})
