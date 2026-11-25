import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Library, TailorRun, TailorRunSummary, TailoredResumeData } from '../../api/types'
import { renderWithProviders } from '../../test/render'
import { POLL_MS, ResumePanel } from './ResumePanel'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const SUMMARY_RUNNING: TailorRunSummary = {
  run_id: 'run-1',
  status: 'running',
  stop_reason: null,
  started_at: '2026-11-25T10:00:00Z',
  finished_at: null,
  has_resume: false,
}

const RUN_RUNNING: TailorRun = {
  run_id: 'run-1',
  job_id: 'job-1',
  base_doc_id: 'doc-1',
  fact_set_id: 'set-1',
  status: 'running',
  stop_reason: null,
  started_at: '2026-11-25T10:00:00Z',
  finished_at: null,
  blocks: [],
  bullets: [],
}

const RUN_SUCCEEDED: TailorRun = {
  ...RUN_RUNNING,
  status: 'succeeded',
  finished_at: '2026-11-25T10:00:12Z',
  blocks: [
    {
      key: 'b1',
      experience: '问答项目',
      status: 'done',
      focus: [
        { text: '熟悉 FastAPI 接口开发', type: 'hard', base_status: 'written' },
        { text: '补充 pgvector 混合检索指标', type: 'hard', base_status: 'suggest_add' },
        { text: '有 LangGraph 项目经验', type: 'preferred', base_status: 'to_check' },
      ],
    },
  ],
}

const RESUME_DATA: TailoredResumeData = {
  resume_id: 'res-1',
  run_id: 'run-1',
  version_id: 'ver-1',
  version: 1,
  current_version_id: 'ver-1',
  versions: [{ version_id: 'ver-1', version: 1, created_at: '2026-11-25T10:00:12Z' }],
  content: {
    header: ['候选人'],
    style: { font: '', size_pt: 10.5, leading: 1.5 },
    sections: [
      {
        title: '项目经历',
        kind: 'project',
        entries: [
          {
            entry_id: 'e1',
            experience: '问答项目',
            heading: '问答项目',
            title: '问答项目',
            time: null,
            lines: [],
            bullets: [
              {
                bullet_id: 'bul-pass',
                text: '基于 FastAPI 实现问答接口，返回引用段落编号',
                fact_ids: ['f-1'],
                origin: 'passed',
                edited: false,
                check: {
                  text: '基于 FastAPI 实现问答接口，返回引用段落编号',
                  layers: 'full',
                  issues: [],
                },
              },
              {
                bullet_id: 'bul-fallback',
                text: '主导三人小组交付设备手册问答原型',
                fact_ids: ['f-2'],
                origin: 'fallback',
                edited: false,
                check: {
                  text: '主导三人小组交付设备手册问答原型',
                  layers: 'program',
                  issues: [],
                },
              },
              {
                bullet_id: 'bul-pending',
                text: '系统性能提升 10 倍',
                fact_ids: [],
                origin: 'pending',
                edited: false,
                check: {
                  text: '系统性能提升 10 倍',
                  layers: 'full',
                  issues: [
                    { kind: 'number', message: '数字「10 倍」找不到依据', fragment: '10 倍' },
                  ],
                },
              },
            ],
          },
        ],
      },
    ],
  },
  facts: {
    'f-1': {
      experience: '问答项目',
      text: '负责用 FastAPI 实现问答接口',
      source: 'resume',
      source_quote: '负责用 FastAPI 实现问答接口，在答案里返回引用段落编号',
    },
    'f-2': {
      experience: '问答项目',
      text: '主导三人小组交付设备手册问答原型',
      source: 'user',
      source_quote: null,
    },
  },
}

const LIBRARY: Library = {
  fact_set: { fact_set_id: 'set-1', version: 1 },
  facts: [],
  criteria: [],
  education: [],
  draft: { base_fact_set_id: 'set-1', items: [], criteria: [], education: [], revision: 0 },
}

describe('ResumePanel 轮询停止、依据追溯与底稿没写到的要求', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('运行期间按 POLL_MS 轮询，运行结束后停止不再发请求', async () => {
    vi.useFakeTimers()
    let runCalls = 0
    let finished = false

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/jobs/job-1/tailor-runs') {
        return jsonResponse(200, [
          finished
            ? { ...SUMMARY_RUNNING, status: 'succeeded', has_resume: true }
            : SUMMARY_RUNNING,
        ])
      }
      if (url === '/api/tailor-runs/run-1') {
        runCalls += 1
        if (runCalls >= 3) finished = true
        return jsonResponse(200, runCalls >= 3 ? RUN_SUCCEEDED : RUN_RUNNING)
      }
      if (url === '/api/tailor-runs/run-1/resume') {
        return jsonResponse(200, RESUME_DATA)
      }
      if (url === '/api/facts') {
        return jsonResponse(200, LIBRARY)
      }
      throw new Error(`未预期的请求: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<ResumePanel jobId="job-1" />)

    // 第 1 次轮询：running
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(runCalls).toBe(1)

    // 第 2 次轮询：仍为 running
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS)
    })
    expect(runCalls).toBe(2)

    // 第 3 次轮询：转为 succeeded
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS)
    })
    expect(runCalls).toBe(3)

    // 运行已结束：再推进 5 个轮询周期，不能再发 GET /api/tailor-runs/run-1
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 5)
    })
    expect(runCalls).toBe(3)
  })

  it('支持依据追溯切换条目，并列出底稿没写到的要求（含你补充的事实）', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/jobs/job-1/tailor-runs') {
        return jsonResponse(200, [{ ...SUMMARY_RUNNING, status: 'succeeded', has_resume: true }])
      }
      if (url === '/api/tailor-runs/run-1') return jsonResponse(200, RUN_SUCCEEDED)
      if (url === '/api/tailor-runs/run-1/resume') return jsonResponse(200, RESUME_DATA)
      if (url === '/api/facts') return jsonResponse(200, LIBRARY)
      throw new Error(`未预期的请求: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<ResumePanel jobId="job-1" />)

    await waitFor(() =>
      expect(
        screen.getByText(/改写 3 条 · 通过 1 条 · 回退原文 1 条 · 待处理 1 条/),
      ).toBeInTheDocument(),
    )

    // 进入依据追溯
    await user.click(screen.getByRole('tab', { name: '依据追溯' }))
    expect(await screen.findByText(/原文片段：负责用 FastAPI 实现问答接口/)).toBeInTheDocument()
    expect(screen.getByText('来自简历')).toBeInTheDocument()

    // 点开回退条目
    await user.click(screen.getByRole('button', { name: /主导三人小组交付设备手册问答原型/ }))
    expect(await screen.findByText(/检查结论：改写没通过检查，用了事实原文/)).toBeInTheDocument()
    expect(screen.getByText('你补充的')).toBeInTheDocument()

    // 点开待处理条目
    await user.click(screen.getByRole('button', { name: /系统性能提升 10 倍/ }))
    expect(await screen.findByText(/需要你手动修改或删除/)).toBeInTheDocument()
    expect(screen.getByText('数字「10 倍」找不到依据')).toBeInTheDocument()

    // 底稿没写到：别的简历写过的、你补充的都列出；底稿已写到的不列
    await user.click(screen.getByRole('tab', { name: '底稿没写到 (2)' }))
    expect(await screen.findByText('补充 pgvector 混合检索指标')).toBeInTheDocument()
    expect(screen.getByText('别的简历写过')).toBeInTheDocument()
    expect(screen.getByText('有 LangGraph 项目经验')).toBeInTheDocument()
    expect(screen.getByText('你补充的，待核对')).toBeInTheDocument()
    expect(screen.queryByText('熟悉 FastAPI 接口开发')).not.toBeInTheDocument()
    // 说明部分覆盖、未覆盖去哪里看
    expect(screen.getByText(/部分覆盖、未覆盖的要求见匹配报告/)).toBeInTheDocument()
  })

  it('依据追溯区分自动回退与用户主动改用事实原文', async () => {
    const user = userEvent.setup()
    const withUserChoice: TailoredResumeData = {
      ...RESUME_DATA,
      content: {
        ...RESUME_DATA.content,
        sections: [
          {
            ...RESUME_DATA.content.sections[0],
            entries: [
              {
                ...RESUME_DATA.content.sections[0].entries[0],
                bullets: [
                  RESUME_DATA.content.sections[0].entries[0].bullets[1],
                  {
                    bullet_id: 'bul-user-facts',
                    text: '负责用 FastAPI 实现问答接口',
                    fact_ids: ['f-1'],
                    origin: 'fallback',
                    user_choice: 'use_facts',
                    edited: false,
                    check: {
                      text: '负责用 FastAPI 实现问答接口',
                      layers: 'program',
                      issues: [],
                    },
                  },
                ],
              },
            ],
          },
        ],
      },
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/jobs/job-1/tailor-runs') {
        return jsonResponse(200, [{ ...SUMMARY_RUNNING, status: 'succeeded', has_resume: true }])
      }
      if (url === '/api/tailor-runs/run-1') return jsonResponse(200, RUN_SUCCEEDED)
      if (url === '/api/tailor-runs/run-1/resume') return jsonResponse(200, withUserChoice)
      if (url === '/api/facts') return jsonResponse(200, LIBRARY)
      throw new Error(`未预期的请求: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<ResumePanel jobId="job-1" />)
    await user.click(await screen.findByRole('tab', { name: '依据追溯' }))

    // 第一条为自动回退
    expect(await screen.findByText(/检查结论：改写没通过检查，用了事实原文/)).toBeInTheDocument()
    expect(screen.getByText('已回退原文')).toBeInTheDocument()

    // 点开用户主动改用事实原文条目
    await user.click(screen.getByRole('button', { name: /负责用 FastAPI 实现问答接口/ }))
    expect(await screen.findByText(/检查结论：你选择了改用事实原文/)).toBeInTheDocument()
    expect(screen.getByText('已改用事实原文')).toBeInTheDocument()
  })
})
