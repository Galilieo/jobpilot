import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MemoryItem } from '../../api/types'
import { jsonResponse, stubFetch } from '../../test/fetchMock'
import { renderWithProviders } from '../../test/render'
import { MemoryList } from './MemoryList'

function makeMemories(overrides: Partial<Record<string, Partial<MemoryItem>>> = {}): MemoryItem[] {
  const base: MemoryItem[] = [
    {
      key: 'target_direction',
      label: '目标方向',
      group: 'job',
      type: 'text',
      options: [],
      max_length: 100,
      max_items: null,
      value: null,
      revision: 0,
      deleted: false,
    },
    {
      key: 'cities',
      label: '城市',
      group: 'job',
      type: 'list',
      options: [],
      max_length: 20,
      max_items: 5,
      value: ['上海', '杭州'],
      revision: 1,
      deleted: false,
    },
    {
      key: 'excluded_types',
      label: '排除类型',
      group: 'job',
      type: 'list',
      options: [],
      max_length: 20,
      max_items: 10,
      value: null,
      revision: 2,
      deleted: true,
    },
    {
      key: 'job_notes',
      label: '其他求职偏好',
      group: 'job',
      type: 'text',
      options: [],
      max_length: 300,
      max_items: null,
      value: '不接受大小周',
      revision: 1,
      deleted: false,
    },
    {
      key: 'tone',
      label: '语气',
      group: 'writing',
      type: 'enum',
      options: ['简洁', '平实', '偏技术细节'],
      max_length: null,
      max_items: null,
      value: '平实',
      revision: 1,
      deleted: false,
    },
    {
      key: 'bullet_length',
      label: '每条长度',
      group: 'writing',
      type: 'enum',
      options: ['短', '适中', '完整'],
      max_length: null,
      max_items: null,
      value: null,
      revision: 0,
      deleted: false,
    },
    {
      key: 'tech_terms',
      label: '技术名写法',
      group: 'writing',
      type: 'enum',
      options: ['原文', '英文优先', '中文优先'],
      max_length: null,
      max_items: null,
      value: '原文',
      revision: 1,
      deleted: false,
    },
  ]
  return base.map((item) => ({ ...item, ...overrides[item.key] }))
}

describe('MemoryList 我的记忆页', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('显示顶部说明、两组七栏以及已删除状态', async () => {
    stubFetch({
      'GET /api/memories': () => jsonResponse(200, makeMemories()),
    })
    renderWithProviders(<MemoryList />)

    expect(
      await screen.findByText('记忆只影响建议和措辞，不会被当作经历写进简历'),
    ).toBeInTheDocument()
    expect(screen.getByText('求职偏好')).toBeInTheDocument()
    expect(screen.getByText('写作偏好')).toBeInTheDocument()
    for (const label of [
      '目标方向',
      '城市',
      '排除类型',
      '其他求职偏好',
      '语气',
      '每条长度',
      '技术名写法',
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText('已删除')).toBeInTheDocument()
  })

  it('支持文本、列表和枚举三种类型编辑并携带 expected_revision 保存，已删除栏也可重填', async () => {
    const user = userEvent.setup()
    let data = makeMemories()
    const calls = stubFetch({
      'GET /api/memories': () => jsonResponse(200, data),
      'PUT /api/memories/target_direction': (body) => {
        const payload = body as { expected_revision: number; value: string }
        const updated = {
          ...data[0],
          value: payload.value,
          revision: payload.expected_revision + 1,
          deleted: false,
        }
        data = data.map((item) => (item.key === 'target_direction' ? updated : item))
        return jsonResponse(200, updated)
      },
      'PUT /api/memories/excluded_types': (body) => {
        const payload = body as { expected_revision: number; value: string[] }
        const updated = {
          ...data[2],
          value: payload.value,
          revision: payload.expected_revision + 1,
          deleted: false,
        }
        data = data.map((item) => (item.key === 'excluded_types' ? updated : item))
        return jsonResponse(200, updated)
      },
      'PUT /api/memories/tone': (body) => {
        const payload = body as { expected_revision: number; value: string }
        const updated = {
          ...data[4],
          value: payload.value,
          revision: payload.expected_revision + 1,
          deleted: false,
        }
        data = data.map((item) => (item.key === 'tone' ? updated : item))
        return jsonResponse(200, updated)
      },
    })

    renderWithProviders(<MemoryList />)
    await screen.findByText('目标方向')

    // 1) 文本型：target_direction
    const targetRow = screen.getByTestId('memory-row-target_direction')
    await user.click(within(targetRow).getByRole('button', { name: /编\s?辑/ }))
    await user.type(within(targetRow).getByLabelText('目标方向'), 'Agent 应用开发')
    await user.click(within(targetRow).getByRole('button', { name: /保\s?存/ }))
    await waitFor(() => {
      const putCall = calls.find((c) => c.route === 'PUT /api/memories/target_direction')
      expect(putCall?.body).toEqual({ expected_revision: 0, value: 'Agent 应用开发' })
    })

    // 2) 已删除列表型恢复：excluded_types
    const excludedRow = screen.getByTestId('memory-row-excluded_types')
    await user.click(within(excludedRow).getByRole('button', { name: /重新填写|编\s?辑/ }))
    const tagInput = within(excludedRow).getByRole('combobox')
    await user.type(tagInput, '外包{Enter}')
    await user.click(within(excludedRow).getByRole('button', { name: /保\s?存/ }))
    await waitFor(() => {
      const putCall = calls.find((c) => c.route === 'PUT /api/memories/excluded_types')
      expect(putCall?.body).toEqual({ expected_revision: 2, value: ['外包'] })
    })

    // 3) 枚举型：tone
    const toneRow = screen.getByTestId('memory-row-tone')
    await user.click(within(toneRow).getByRole('button', { name: /编\s?辑/ }))
    await user.click(within(toneRow).getByRole('combobox'))
    const toneOptions = await screen.findAllByText('偏技术细节')
    await user.click(toneOptions[toneOptions.length - 1])
    await user.click(within(toneRow).getByRole('button', { name: /保\s?存/ }))
    await waitFor(() => {
      const putCall = calls.find((c) => c.route === 'PUT /api/memories/tone')
      expect(putCall?.body).toEqual({ expected_revision: 1, value: '偏技术细节' })
    })
  })

  it('删除前先二次确认，确认后才带版本调 DELETE', async () => {
    const user = userEvent.setup()
    let data = makeMemories()
    const calls = stubFetch({
      'GET /api/memories': () => jsonResponse(200, data),
      'DELETE /api/memories/cities': (body) => {
        const payload = body as { expected_revision: number }
        const updated = {
          ...data[1],
          value: null,
          revision: payload.expected_revision + 1,
          deleted: true,
        }
        data = data.map((item) => (item.key === 'cities' ? updated : item))
        return jsonResponse(200, updated)
      },
    })

    renderWithProviders(<MemoryList />)
    const citiesRow = await screen.findByTestId('memory-row-cities')
    await user.click(within(citiesRow).getByRole('button', { name: /删\s?除/ }))

    // 二次确认弹框弹出前，不能立刻发出 DELETE
    expect(calls.some((c) => c.route === 'DELETE /api/memories/cities')).toBe(false)
    const confirmBtn = await screen.findByRole('button', { name: /确认删除/ })
    await user.click(confirmBtn)

    await waitFor(() => {
      const delCall = calls.find((c) => c.route === 'DELETE /api/memories/cities')
      expect(delCall?.body).toEqual({ expected_revision: 1 })
    })
  })

  it('保存遇到 409 时重新读取并提示最新内容', async () => {
    const user = userEvent.setup()
    let getCount = 0
    stubFetch({
      'GET /api/memories': () => {
        getCount += 1
        return jsonResponse(
          200,
          makeMemories(
            getCount > 1 ? { target_direction: { value: '搜索推荐算法', revision: 2 } } : {},
          ),
        )
      },
      'PUT /api/memories/target_direction': () => jsonResponse(409, { detail: '内容已在别处更新' }),
    })

    renderWithProviders(<MemoryList />)
    const row = await screen.findByTestId('memory-row-target_direction')
    await user.click(within(row).getByRole('button', { name: /编\s?辑/ }))
    await user.type(within(row).getByLabelText('目标方向'), '过时修改')
    await user.click(within(row).getByRole('button', { name: /保\s?存/ }))

    expect(await screen.findByText('这一栏刚被别处改过，已显示最新内容')).toBeInTheDocument()
    expect(await screen.findByText('搜索推荐算法')).toBeInTheDocument()
  })
})
