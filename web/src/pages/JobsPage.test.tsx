import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../test/render'
import { JobsPage } from './JobsPage'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  return input instanceof URL ? input.toString() : input.url
}

function idempotencyKeyOf(call: [RequestInfo | URL, RequestInit?]): string {
  return (call[1]?.headers as Record<string, string>)['Idempotency-Key']
}

describe('JobsPage 添加岗位多种输入方式与幂等重试', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('文本框为空或只有空白时不能提交，输入内容后才能点击解析岗位', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(200, [])),
    )

    renderWithProviders(<JobsPage />)
    await user.click(await screen.findByRole('button', { name: '添加岗位' }))

    const submit = await screen.findByRole('button', { name: '解析岗位' })
    const textarea = screen.getByPlaceholderText('JD 原文')
    expect(submit).toBeDisabled()

    await user.type(textarea, '   \n  ')
    expect(submit).toBeDisabled()

    await user.type(textarea, 'Python 后端实习生')
    expect(submit).toBeEnabled()
  })

  it('上传截图后识别结果进入文本框并允许核对修改再提交', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (input) => {
        const url = requestUrl(input)
        if (url === '/api/jobs') return jsonResponse(200, [])
        if (url === '/api/ocr') {
          return jsonResponse(200, { text: '第二段：熟悉 FastAPI\n\n第一段：后端实习生' })
        }
        return jsonResponse(404, { detail: 'unexpected' })
      },
    )
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<JobsPage />)
    await user.click(await screen.findByRole('button', { name: '添加岗位' }))
    await user.click(screen.getByText('上传截图'))

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const files = [
      new File(['img1'], 'part2.png', { type: 'image/png' }),
      new File(['img2'], 'part1.png', { type: 'image/png' }),
    ]
    await user.upload(fileInput, files)

    const textarea = screen.getByPlaceholderText('JD 原文')
    await waitFor(() => expect(textarea).toHaveValue('第二段：熟悉 FastAPI\n\n第一段：后端实习生'))

    const ocrCall = fetchMock.mock.calls.find((call) => requestUrl(call[0]) === '/api/ocr')
    expect(ocrCall).toBeDefined()
    const form = ocrCall?.[1]?.body as FormData
    expect(form.getAll('files')).toHaveLength(2)

    // 用户在文本框核对调整顺序后再提交，提交的应是修改后的文字
    fetchMock.mockImplementation(async (input, init) => {
      const url = requestUrl(input)
      if (url === '/api/jobs' && init?.method === 'POST') {
        return jsonResponse(201, { job_id: 'job-ocr-1' })
      }
      return jsonResponse(200, [])
    })
    await user.clear(textarea)
    await user.type(textarea, '第一段：后端实习生\n第二段：熟悉 FastAPI')
    await user.click(screen.getByRole('button', { name: '解析岗位' }))

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        (call) => requestUrl(call[0]) === '/api/jobs' && call[1]?.method === 'POST',
      )
      expect(postCall).toBeDefined()
      expect(JSON.parse(postCall?.[1]?.body as string)).toEqual({
        raw_text: '第一段：后端实习生\n第二段：熟悉 FastAPI',
      })
    })
  })

  it('上传文件提取文字进入文本框，超过 10 张截图时直接提示不请求识别', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (input) => {
        const url = requestUrl(input)
        if (url === '/api/jobs') return jsonResponse(200, [])
        if (url === '/api/extract-text') return jsonResponse(200, { text: 'DOCX 提取的 JD' })
        return jsonResponse(404, { detail: 'unexpected' })
      },
    )
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<JobsPage />)
    await user.click(await screen.findByRole('button', { name: '添加岗位' }))
    await user.click(screen.getByText('上传文件'))

    const docInput = document.querySelector('input[type="file"]') as HTMLInputElement
    await user.upload(docInput, new File(['docx'], 'jd.docx', { type: 'application/octet-stream' }))

    const textarea = screen.getByPlaceholderText('JD 原文')
    await waitFor(() => expect(textarea).toHaveValue('DOCX 提取的 JD'))

    await user.click(screen.getByText('上传截图'))
    const imgInput = document.querySelector('input[type="file"]') as HTMLInputElement
    const tooMany = Array.from(
      { length: 11 },
      (_, index) => new File(['img'], `${index}.png`, { type: 'image/png' }),
    )
    await user.upload(imgInput, tooMany)

    expect(await screen.findByText('一次最多上传 10 张截图')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some((call) => requestUrl(call[0]) === '/api/ocr')).toBe(false)
  })

  it('提交失败保留输入并在同一次重试复用幂等键，成功后清空并换新键', async () => {
    const user = userEvent.setup()
    let postCount = 0
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async (input, init) => {
        const url = requestUrl(input)
        if (url === '/api/jobs' && init?.method === 'POST') {
          postCount += 1
          if (postCount === 1) return jsonResponse(502, { detail: 'JD 解析失败，请稍后重试' })
          return jsonResponse(201, { job_id: `job-${postCount}` })
        }
        return jsonResponse(200, [])
      },
    )
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<JobsPage />)
    await user.click(await screen.findByRole('button', { name: '添加岗位' }))

    const textarea = screen.getByPlaceholderText('JD 原文')
    const submitButton = screen.getByRole('button', { name: '解析岗位' })

    // 第一次提交失败：保留输入，不关弹窗
    await user.type(textarea, '第一份合成 JD 正文')
    await user.click(submitButton)

    expect(await screen.findByText('JD 解析失败，请稍后重试')).toBeInTheDocument()
    expect(textarea).toHaveValue('第一份合成 JD 正文')

    const postCalls = (): [RequestInfo | URL, RequestInit?][] =>
      fetchMock.mock.calls.filter(
        (call) => requestUrl(call[0]) === '/api/jobs' && call[1]?.method === 'POST',
      )
    expect(postCalls()).toHaveLength(1)
    const firstKey = idempotencyKeyOf(postCalls()[0])
    expect(firstKey).toBeTruthy()

    // 重试：必须复用同一个幂等键
    await user.click(submitButton)
    await waitFor(() => expect(postCalls()).toHaveLength(2))
    expect(idempotencyKeyOf(postCalls()[1])).toBe(firstKey)

    // 再次打开弹窗：已清空上次输入；提交新 JD 必须更换新的幂等键
    await user.click(screen.getByRole('button', { name: '添加岗位' }))
    const reopenedTextarea = screen.getByPlaceholderText('JD 原文')
    await waitFor(() => expect(reopenedTextarea).toHaveValue(''))
    await user.type(reopenedTextarea, '第二份合成 JD 正文')
    await user.click(screen.getByRole('button', { name: '解析岗位' }))

    await waitFor(() => expect(postCalls()).toHaveLength(3))
    const nextKey = idempotencyKeyOf(postCalls()[2])
    expect(nextKey).toBeTruthy()
    expect(nextKey).not.toBe(firstKey)
  })

  it('卡片展示薪资、公司·地点·到岗·学历与匹配摘要', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(200, [
          {
            job_id: 'job-matched',
            title: '大模型应用开发实习生',
            company: '青梧数智',
            salary: '250-300 元/天',
            location: '上海',
            onsite_days_per_week: '每周 4 天',
            education: '本科',
            stage: 'saved',
            keywords: ['Python', 'FastAPI', 'RAG', 'Docker'],
            injection_count: 0,
            match_summary: { hard_covered: 4, hard_total: 9, expression_gap: 1 },
            updated_at: '2026-11-25T10:00:00Z',
          },
          {
            job_id: 'job-unmatched',
            title: '后端实习生',
            company: '星桥数据',
            salary: null,
            location: '杭州',
            onsite_days_per_week: null,
            education: null,
            stage: 'applied',
            keywords: ['Go'],
            injection_count: 0,
            match_summary: null,
            updated_at: '2026-11-25T09:00:00Z',
          },
        ]),
      ),
    )

    renderWithProviders(<JobsPage />)

    expect(await screen.findByText('250-300 元/天')).toBeInTheDocument()
    expect(screen.getByText('青梧数智 · 上海 · 每周 4 天 · 本科')).toBeInTheDocument()
    expect(screen.getByText('硬性 4/9 · 表达缺口 1')).toBeInTheDocument()
    expect(screen.getByText('+1')).toBeInTheDocument()

    expect(screen.getByText('薪资未填写')).toBeInTheDocument()
    expect(screen.getByText('星桥数据 · 杭州')).toBeInTheDocument()
    expect(screen.getByText('还没匹配')).toBeInTheDocument()
  })
})
