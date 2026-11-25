import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Library } from '../../api/types'
import { renderWithProviders } from '../../test/render'
import { FactLibrary } from './FactLibrary'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const INITIAL_LIBRARY: Library = {
  fact_set: { fact_set_id: 'set-1', version: 1 },
  facts: [
    {
      fact_id: 'fact-1',
      lineage_id: 'lin-pub-1',
      experience: '后端实习',
      kind: 'action',
      responsibility: 'own',
      text: '负责查询接口实现与单测',
      source: 'resume',
      source_quote: '负责查询接口实现与单测',
    },
  ],
  criteria: [],
  education: [],
  draft: {
    base_fact_set_id: 'set-1',
    revision: 3,
    criteria: [],
    education: [],
    items: [
      {
        lineage_id: 'lin-draft-1',
        status: 'pending',
        experience: '问答项目',
        section: 'project',
        kind: 'technology',
        responsibility: 'own',
        text: '使用 pgvector 存储文档向量',
        source: 'resume',
        source_quote: '使用 pgvector 存储文档向量',
        quote_verified: true,
        review_reasons: [],
      },
    ],
  },
}

const REFRESHED_LIBRARY: Library = {
  ...INITIAL_LIBRARY,
  draft: {
    ...INITIAL_LIBRARY.draft,
    revision: 4,
    items: [
      {
        ...INITIAL_LIBRARY.draft.items[0],
        text: '使用 PostgreSQL 与 pgvector 存储文档向量',
      },
    ],
  },
}

describe('FactLibrary 409 冲突刷新、手动补充与编辑已发布事实', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('确认事实遇到 409 冲突时自动刷新事实库并弹出提示', async () => {
    const user = userEvent.setup()
    let getCount = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/facts' && (!init?.method || init.method === 'GET')) {
        getCount += 1
        return jsonResponse(200, getCount === 1 ? INITIAL_LIBRARY : REFRESHED_LIBRARY)
      }
      if (url === '/api/facts/lin-draft-1' && init?.method === 'PUT') {
        return jsonResponse(409, {
          detail: { message: '草稿已在别处更新，请刷新后再操作', revision: 4 },
        })
      }
      throw new Error(`未预期的请求: ${init?.method ?? 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<FactLibrary />)

    expect(await screen.findByText('使用 pgvector 存储文档向量')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /确认$/ }))

    expect(
      await screen.findByText('事实库已在别处更新，已为你刷新，请再看一眼后操作'),
    ).toBeInTheDocument()
    await waitFor(() => expect(getCount).toBeGreaterThanOrEqual(2))
    expect(await screen.findByText('使用 PostgreSQL 与 pgvector 存储文档向量')).toBeInTheDocument()
  })

  it('支持手动补充事实并标为「你补充的」，也支持修改已发布事实进入草稿', async () => {
    const user = userEvent.setup()
    let libraryState: Library = structuredClone(INITIAL_LIBRARY)
    const calls: { method: string; url: string; body: unknown }[] = []

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      calls.push({ method, url, body })

      if (url === '/api/facts' && method === 'GET') {
        return jsonResponse(200, libraryState)
      }
      if (url === '/api/facts' && method === 'POST') {
        const created = {
          lineage_id: 'lin-user-1',
          status: 'confirmed' as const,
          experience: (body as { experience: string }).experience,
          section: null,
          kind: (body as { kind: 'result' }).kind,
          responsibility: (body as { responsibility: 'na' }).responsibility,
          text: (body as { text: string }).text,
          source: 'user' as const,
          source_quote: null,
          quote_verified: false,
          review_reasons: [],
        }
        libraryState = {
          ...libraryState,
          draft: {
            ...libraryState.draft,
            revision: 4,
            items: [...libraryState.draft.items, created],
          },
        }
        return jsonResponse(201, { ...created, revision: 4 })
      }
      if (url === '/api/facts/lin-pub-1' && method === 'PUT') {
        const updated = {
          lineage_id: 'lin-pub-1',
          status: 'pending' as const,
          experience: '后端实习',
          section: 'internship' as const,
          kind: 'action' as const,
          responsibility: (body as { responsibility: 'lead' }).responsibility,
          text: '负责查询接口实现与单测',
          source: 'resume' as const,
          source_quote: '负责查询接口实现与单测',
          quote_verified: true,
          review_reasons: [],
        }
        libraryState = {
          ...libraryState,
          draft: {
            ...libraryState.draft,
            revision: 5,
            items: [...libraryState.draft.items, updated],
          },
        }
        return jsonResponse(200, { ...updated, revision: 5 })
      }
      throw new Error(`未预期的请求: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<FactLibrary />)
    expect(await screen.findByText('负责查询接口实现与单测')).toBeInTheDocument()

    // 手动补充事实
    await user.click(screen.getByRole('button', { name: /补充事实/ }))
    await screen.findByRole('dialog')
    await user.type(screen.getByRole('combobox', { name: '所属经历' }), '求职条件')
    await user.type(
      screen.getByPlaceholderText(
        '例如：对查询接口做压测与优化，吞吐量从约 200 QPS 提升到约 800 QPS',
      ),
      '每周可到岗 5 天',
    )
    await user.click(screen.getByRole('button', { name: '加入草稿' }))

    expect(await screen.findByText('每周可到岗 5 天')).toBeInTheDocument()
    expect(screen.getByText('你补充的')).toBeInTheDocument()
    const postCall = calls.find((c) => c.method === 'POST' && c.url === '/api/facts')
    expect(postCall?.body).toMatchObject({
      experience: '求职条件',
      text: '每周可到岗 5 天',
    })

    // 修改已发布事实的职责程度，带上期望修订号 4 进入草稿
    const pubSelect = screen.getByRole('combobox', { name: '已发布事实职责程度' })
    await user.click(pubSelect)
    const leadOptions = await screen.findAllByText('主导')
    await user.click(leadOptions[leadOptions.length - 1])

    expect(await screen.findByText('草稿中已修改')).toBeInTheDocument()
    const putCall = calls.find((c) => c.method === 'PUT' && c.url === '/api/facts/lin-pub-1')
    expect(putCall?.body).toEqual({ responsibility: 'lead', expected_revision: 4 })
  })

  it('求职条件 409 刷新保留本地输入，且删除事实携带修订号', async () => {
    const user = userEvent.setup()
    let libraryState: Library = structuredClone(INITIAL_LIBRARY)
    const calls: { method: string; url: string }[] = []

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ method, url })

      if (url === '/api/facts' && method === 'GET') {
        return jsonResponse(200, libraryState)
      }
      if (url === '/api/facts/criteria' && method === 'PUT') {
        libraryState = {
          ...libraryState,
          draft: { ...libraryState.draft, revision: 9 },
        }
        return jsonResponse(409, {
          detail: { message: '草稿已在别处更新，请刷新后再操作', revision: 9 },
        })
      }
      if (url.startsWith('/api/facts/lin-draft-1') && method === 'DELETE') {
        libraryState = {
          ...libraryState,
          draft: { ...libraryState.draft, items: [], revision: 10 },
        }
        return new Response(null, { status: 204, headers: { 'X-Draft-Revision': '10' } })
      }
      throw new Error(`未预期的请求: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<FactLibrary />)
    expect(await screen.findByText('每周可到岗天数')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '编辑最早到岗时间' }))
    const input = screen.getByRole('textbox', { name: '最早到岗时间' })
    await user.type(input, '12 月下旬')
    await user.click(screen.getByRole('button', { name: '保存并确认' }))

    expect(
      await screen.findByText('事实库已在别处更新，已为你刷新，请再看一眼后操作'),
    ).toBeInTheDocument()
    // 409 刷新后本地输入保留在输入框中。
    expect(screen.getByRole('textbox', { name: '最早到岗时间' })).toHaveValue('12 月下旬')

    // 删除草稿事实时携带最新修订号 9。
    await user.click(screen.getByRole('button', { name: '删除' }))
    await user.click(await screen.findByRole('button', { name: '删 除' }))
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/facts/lin-draft-1?expected_revision=9')).toBe(true),
    )
  })
})
