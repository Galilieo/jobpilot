import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MatchReport, MatchSummary } from '../../api/types'
import { renderWithProviders } from '../../test/render'
import { MatchPanel } from './MatchPanel'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const REPORT: MatchReport = {
  report_id: 'rep-1',
  fact_set_version: 2,
  hard_summary: { total: 2, covered: 2, expression_gap: 0, partial: 0, missing: 0 },
  preferred_summary: { total: 0, covered: 0, expression_gap: 0, partial: 0, missing: 0 },
  main_gaps: [],
  created_at: '2026-11-25T10:00:00Z',
  items: [
    {
      requirement_id: 'req-days',
      requirement_type: 'hard',
      requirement_text: '每周至少到岗 4 天',
      verdict: 'covered',
      facts: [],
      criteria: [
        {
          ref_id: 'criteria.onsite_days_per_week',
          kind: 'criterion',
          label: '求职条件',
          text: '每周可到岗 5 天',
        },
      ],
      decision_source: 'program',
      review_note: null,
      reason: '已确认每周可到岗天数为 5 天/周，满足岗位要求。',
      suggestion: '求职条件已满足该要求，无需额外改写进简历正文。',
    },
    {
      requirement_id: 'req-compound',
      requirement_type: 'hard',
      requirement_text: '本科及以上，熟悉 Python',
      verdict: 'covered',
      facts: [],
      criteria: [],
      decision_source: 'model',
      review_note: '复合要求包含其他内容（熟悉 Python），不由程序单独判定',
      reason: '满足要求',
      suggestion: '保留',
    },
  ],
}

const SUMMARIES: MatchSummary[] = [
  {
    report_id: REPORT.report_id,
    fact_set_version: REPORT.fact_set_version,
    hard_summary: REPORT.hard_summary,
    preferred_summary: REPORT.preferred_summary,
    main_gaps: REPORT.main_gaps,
    created_at: REPORT.created_at,
  },
]

describe('MatchPanel 求职条件依据与程序判定展示', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染求职条件依据、按数字比对标签以及待核对说明', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/jobs/job-1/matches') return jsonResponse(200, SUMMARIES)
      if (url === '/api/match-reports/rep-1') return jsonResponse(200, REPORT)
      throw new Error(`未预期的请求: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<MatchPanel jobId="job-1" />)

    expect(await screen.findByText('依据：求职条件 · 每周可到岗 5 天')).toBeInTheDocument()
    expect(screen.getByText('按数字比对')).toBeInTheDocument()
    expect(screen.getByText('待核对')).toBeInTheDocument()
    expect(
      screen.getByText('待核对说明：复合要求包含其他内容（熟悉 Python），不由程序单独判定'),
    ).toBeInTheDocument()
  })
})
