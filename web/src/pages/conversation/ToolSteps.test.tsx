import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { describeSteps, groupSteps } from './stepGroups'
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
        summary: '共 5 项',
        durationMs: 400,
      },
      {
        key: 'r1',
        tool: 'read_facts',
        status: 'success',
        count: 2,
        jobIds: [],
        summary: '完成',
        durationMs: 600,
      },
      {
        key: 'r2',
        tool: 'read_job',
        status: 'success',
        count: 3,
        jobIds: ['j1', 'j2'],
        summary: '完成',
        durationMs: 600,
      },
      {
        key: 'r6',
        tool: 'read_job',
        status: 'error',
        count: 1,
        jobIds: [],
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
