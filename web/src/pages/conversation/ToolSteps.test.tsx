import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { describeSteps, detailLines, groupSteps } from './stepGroups'
import { ToolSteps, type StepView } from './ToolSteps'

const READS: StepView[] = [
  { key: 'r0', tool: 'list_jobs', status: 'success', result: { total: 5 }, durationMs: 400 },
  { key: 'r1', tool: 'read_facts', status: 'success', result: {}, durationMs: 300 },
  { key: 'r2', tool: 'read_job', status: 'success', result: { job_id: 'j1' }, durationMs: 200 },
  { key: 'r3', tool: 'read_facts', status: 'success', result: {}, durationMs: 300 },
  { key: 'r4', tool: 'read_job', status: 'success', result: { job_id: 'j2' }, durationMs: 200 },
  { key: 'r5', tool: 'read_job', status: 'success', result: { job_id: 'j1' }, durationMs: 200 },
  { key: 'r6', tool: 'read_job', status: 'error', result: { error: '岗位不存在' } },
]

const NAMES: Record<string, string> = { j1: 'Java 后端', j2: 'AI 应用开发', j3: '数据开发' }
const title = (id: string) => NAMES[id]

describe('工具步骤的整理', () => {
  it('同一动作不论是否相邻都合成一组，岗位去重；出错的单独成组', () => {
    expect(groupSteps(READS)).toEqual([
      {
        key: 'r0',
        tool: 'list_jobs',
        status: 'success',
        count: 1,
        jobIds: [],
        calls: [{ jobId: null, detail: null }],
        summary: '共 5 项',
        durationMs: 400,
      },
      {
        key: 'r1',
        tool: 'read_facts',
        status: 'success',
        count: 2,
        jobIds: [],
        calls: [
          { jobId: null, detail: null },
          { jobId: null, detail: null },
        ],
        summary: '完成',
        durationMs: 600,
      },
      {
        key: 'r2',
        tool: 'read_job',
        status: 'success',
        count: 3,
        jobIds: ['j1', 'j2'],
        calls: [
          { jobId: 'j1', detail: null },
          { jobId: 'j2', detail: null },
          { jobId: 'j1', detail: null },
        ],
        summary: '完成',
        durationMs: 600,
      },
      {
        key: 'r6',
        tool: 'read_job',
        status: 'error',
        count: 1,
        jobIds: [],
        calls: [{ jobId: null, detail: null }],
        summary: '岗位不存在',
      },
    ])
  })

  it('收起时用一句话概括：读取类合在一起，岗位按个数，出错的另外说', () => {
    expect(describeSteps(groupSteps(READS))).toBe(
      '查看了岗位库，读了你的经历和 2 个岗位，1 步没成功',
    )
    expect(
      describeSteps(
        groupSteps([
          { key: 'a', tool: 'parse_jd', status: 'success', result: { job_id: 'j1' } },
          { key: 'b', tool: 'match_job', status: 'success', result: {} },
          { key: 'c', tool: 'read_facts', status: 'success', result: {} },
          { key: 'd', tool: 'read_match_report', status: 'success', result: {} },
          { key: 'e', tool: 'tailor_resume', status: 'success', result: {} },
        ]),
      ),
    ).toBe('解析了 JD，做了匹配，读了你的经历和匹配报告，生成了定制简历')
  })
})

describe('ToolSteps', () => {
  it('展开跨对话搜索后显示查询、降级状态和原消息链接', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ToolSteps
        steps={[
          {
            key: 'search1',
            tool: 'search_conversations',
            status: 'success',
            result: {
              total: 1,
              detail: {
                query: '每周到岗',
                degraded: true,
                hits: [
                  {
                    conversation_id: 'c1',
                    seq: 12,
                    title: '云岬科技',
                    snippet: '每周需要到岗四天',
                    role: 'user',
                    date: '2026-11-28T01:00:00Z',
                    source: 'keyword',
                  },
                ],
              },
            },
          },
        ]}
      />,
    )

    await user.click(screen.getByRole('button', { name: /搜索了其他对话/ }))
    await user.click(screen.getByRole('button', { name: /搜索其他对话/ }))
    expect(screen.getByText(/每周到岗/)).toBeInTheDocument()
    expect(screen.getByText(/结果可能不全/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /云岬科技/ })).toHaveAttribute(
      'href',
      '/conversations/c1#message-12',
    )
    expect(screen.getByText('每周需要到岗四天')).toBeInTheDocument()
  })

  it('索引未齐但未降级时提示部分新消息仍在建立索引', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ToolSteps
        steps={[
          {
            key: 'search2',
            tool: 'search_conversations',
            status: 'success',
            result: {
              total: 0,
              detail: { query: '项目', degraded: false, index_pending: true, hits: [] },
            },
          },
        ]}
      />,
    )
    await user.click(screen.getByRole('button', { name: /搜索了其他对话/ }))
    await user.click(screen.getByRole('button', { name: /搜索其他对话/ }))
    expect(screen.getByText(/部分新消息仍在建立索引/)).toBeInTheDocument()
  })

  it('来源对话已删除时显示来源已删除且不提供跳转链接', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ToolSteps
        steps={[
          {
            key: 'search3',
            tool: 'search_conversations',
            status: 'success',
            result: {
              total: 1,
              detail: {
                query: '木槿协作',
                degraded: false,
                hits: [
                  {
                    conversation_id: 'c-deleted',
                    seq: 3,
                    source_deleted: true,
                  },
                ],
              },
            },
          },
        ]}
      />,
    )
    await user.click(screen.getByRole('button', { name: /搜索了其他对话/ }))
    await user.click(screen.getByRole('button', { name: /搜索其他对话/ }))
    expect(screen.getByText('来源已删除')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /来源已删除/ })).not.toBeInTheDocument()
  })

  it('完成后默认收起，只显示概括和总用时；点开按动作列出，岗位做成标签', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ToolSteps steps={READS} title={title} />)

    const head = screen.getByRole('button', { name: /查看了岗位库，读了你的经历和 2 个岗位/ })
    expect(head).toHaveAttribute('aria-expanded', 'false')
    expect(head).toHaveTextContent('2 秒')
    expect(screen.queryByText('读取岗位详情')).not.toBeInTheDocument()

    await user.click(head)
    expect(head).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('共 5 项')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Java 后端' })).toHaveAttribute('href', '/jobs/j1')
    expect(screen.getByRole('link', { name: 'AI 应用开发' })).toBeInTheDocument()
    expect(screen.getByText('读取岗位详情没成功：岗位不存在')).toBeInTheDocument()
    expect(screen.queryByText('完成')).not.toBeInTheDocument()
  })

  it('岗位超过两个时只列前两个，其余显示个数', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ToolSteps
        title={title}
        steps={['j1', 'j2', 'j3'].map((id) => ({
          key: id,
          tool: 'read_job',
          status: 'success' as const,
          result: { job_id: id },
        }))}
      />,
    )
    await user.click(screen.getByRole('button', { name: /读了 3 个岗位/ }))
    expect(screen.getAllByRole('link')).toHaveLength(2)
    expect(screen.getByText('+1')).toBeInTheDocument()
  })

  it('运行中写出正在做的动作和第几步', () => {
    renderWithProviders(
      <ToolSteps
        steps={[READS[0], { key: 's2', tool: 'match_job', status: 'loading', result: null }]}
      />,
    )
    expect(screen.getByText('正在匹配…')).toBeInTheDocument()
    expect(screen.getByText('第 2 步')).toBeInTheDocument()
  })
})

const DETAILED: StepView[] = [
  {
    key: 'd0',
    tool: 'list_jobs',
    status: 'success',
    result: {
      total: 2,
      detail: {
        jobs: [
          { job_id: 'j1', title: 'Java 后端', company: '港桥', location: '上海' },
          { job_id: 'j2', title: 'AI 应用开发', company: '星桥', location: null },
        ],
      },
    },
  },
  {
    key: 'd1',
    tool: 'read_facts',
    status: 'success',
    result: { detail: { version: 3, facts: 24, criteria: 2, education: 0 } },
  },
  {
    key: 'd2',
    tool: 'read_job',
    status: 'success',
    result: { job_id: 'j1', detail: { title: 'Java 后端', company: '港桥', location: '上海' } },
  },
  {
    key: 'd3',
    tool: 'read_job',
    status: 'success',
    result: { job_id: 'j1', detail: { title: 'Java 后端', company: '港桥', location: '上海' } },
  },
  {
    key: 'd4',
    tool: 'read_match_report',
    status: 'success',
    result: { detail: { items: 12, main_gaps: ['没有 Kafka 经历'] } },
  },
]

describe('展开看每次调用的细节', () => {
  it('岗位去重、事实库写版本和条数、报告写要求数和主要差距', () => {
    const [jobs, facts, read, report] = groupSteps(DETAILED)
    expect(detailLines(jobs)).toEqual([
      { key: 'j1', text: 'Java 后端 · 港桥 · 上海', jobId: 'j1' },
      { key: 'j2', text: 'AI 应用开发 · 星桥', jobId: 'j2' },
    ])
    expect(detailLines(facts)).toEqual([
      { key: 'facts', text: '第 3 版 · 24 条事实 · 2 条求职条件' },
    ])
    expect(detailLines(read)).toEqual([{ key: 'j1', text: 'Java 后端 · 港桥 · 上海', jobId: 'j1' }])
    expect(detailLines(report)).toEqual([
      { key: 'items', text: '12 项要求逐条对照' },
      { key: 'gap:0', text: '主要差距：没有 Kafka 经历' },
    ])
  })

  it('没有细节的动作不能再展开；有细节的点开逐条列出，岗位可点', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ToolSteps
        steps={[
          ...DETAILED,
          { key: 'm', tool: 'match_job', status: 'success', result: { report_id: 'r1' } },
        ]}
        title={title}
      />,
    )
    await user.click(screen.getByRole('button', { name: /查看了岗位库/ }))

    expect(screen.getByText('匹配岗位')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /匹配岗位/ })).not.toBeInTheDocument()
    const facts = screen.getByRole('button', { name: /读取经历事实/ })
    expect(screen.queryByText(/第 3 版/)).not.toBeInTheDocument()
    await user.click(facts)
    expect(facts).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('第 3 版 · 24 条事实 · 2 条求职条件')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /读取岗位库/ }))
    expect(screen.getByRole('link', { name: 'AI 应用开发 · 星桥' })).toHaveAttribute(
      'href',
      '/jobs/j2',
    )
  })
})

it('自动记录步骤直接显示内容、原话和撤销，刷新后沿用撤销状态', async () => {
  const user = userEvent.setup()
  const steps: StepView[] = [
    {
      key: 'remember-a',
      tool: 'remember',
      status: 'success',
      result: {
        status: 'remembered',
        detail: { content: '不投外包公司', quote: '我不投外包', action_id: 'a1' },
      },
    },
  ]
  const undoCalls: string[] = []
  const onUndo = (id: string) => undoCalls.push(id)
  const render = renderWithProviders(<ToolSteps steps={steps} records={{ undone: [], onUndo }} />)
  expect(screen.getByText('已记住：不投外包公司')).toBeInTheDocument()
  expect(screen.getByText('你说的「我不投外包」')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: /撤\s?销/ }))
  expect(undoCalls).toEqual(['a1'])
  render.unmount()
  renderWithProviders(<ToolSteps steps={steps} records={{ undone: ['a1'], onUndo }} />)
  expect(screen.getByText('已撤销（下一轮起生效）')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /撤\s?销/ })).not.toBeInTheDocument()
})
