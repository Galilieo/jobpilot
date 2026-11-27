import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { groupSteps } from './stepGroups'
import { ToolSteps, type StepView } from './ToolSteps'

const DONE_STEPS: StepView[] = [
  { key: 's1', tool: 'parse_jd', status: 'success', result: { job_id: 'j1' }, durationMs: 3100 },
  {
    key: 's2',
    tool: 'match_job',
    status: 'success',
    result: { report_id: 'r1' },
    durationMs: 4300,
  },
  {
    key: 's3',
    tool: 'tailor_resume',
    status: 'success',
    result: { run_id: 'tr1' },
    durationMs: 13600,
  },
]

describe('ToolSteps 按轮合并与时间轴', () => {
  it('把同一轮多步工具合成一行动作标题与总步数耗时，点开显示时间轴', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ToolSteps steps={DONE_STEPS} />)

    const head = screen.getByRole('button', { name: /解析岗位、匹配、生成简历/ })
    expect(head).toHaveTextContent('3 步')
    expect(head).toHaveTextContent('用时 21 秒')
    expect(head).toHaveAttribute('aria-expanded', 'false')

    await user.click(head)
    expect(head).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('3.1 秒')).toBeInTheDocument()
    expect(screen.getByText('4.3 秒')).toBeInTheDocument()
    expect(screen.getByText('13.6 秒')).toBeInTheDocument()
  })

  it('运行中显示当前正在执行的步骤', () => {
    renderWithProviders(
      <ToolSteps
        steps={[DONE_STEPS[0], { key: 's2', tool: 'match_job', status: 'loading', result: null }]}
      />,
    )
    expect(screen.getByText('正在匹配…')).toBeInTheDocument()
  })

  it('有失败步骤时标出未成功数量', () => {
    renderWithProviders(
      <ToolSteps
        steps={[
          DONE_STEPS[0],
          { key: 's2', tool: 'match_job', status: 'error', result: { error: '超时' } },
        ]}
      />,
    )
    expect(screen.getByRole('button', { name: /解析岗位、匹配/ })).toHaveTextContent('1 步没成功')
  })
})

describe('ToolSteps 合并重复步骤', () => {
  const READS: StepView[] = [
    { key: 'r0', tool: 'list_jobs', status: 'success', result: { total: 5 } },
    { key: 'r1', tool: 'read_facts', status: 'success', result: {} },
    { key: 'r2', tool: 'read_facts', status: 'success', result: {} },
    { key: 'r3', tool: 'read_job', status: 'success', result: { job_id: 'j1' } },
    { key: 'r4', tool: 'read_job', status: 'success', result: { job_id: 'j2' } },
    { key: 'r5', tool: 'read_job', status: 'error', result: { error: '岗位不存在' } },
    { key: 'r6', tool: 'read_job', status: 'success', result: { job_id: 'j1' } },
  ]

  it('相邻的同一工具合成一行，成功和失败分开，岗位按名字列出且不重复', () => {
    expect(groupSteps(READS)).toEqual([
      { key: 'r0', tool: 'list_jobs', status: 'success', count: 1, jobIds: [], summary: '共 5 项' },
      { key: 'r1', tool: 'read_facts', status: 'success', count: 2, jobIds: [], summary: '完成' },
      {
        key: 'r3',
        tool: 'read_job',
        status: 'success',
        count: 2,
        jobIds: ['j1', 'j2'],
        summary: '完成',
      },
      {
        key: 'r5',
        tool: 'read_job',
        status: 'error',
        count: 1,
        jobIds: [],
        summary: '岗位不存在',
      },
      { key: 'r6', tool: 'read_job', status: 'success', count: 1, jobIds: ['j1'], summary: '完成' },
    ])
  })

  it('展开后显示次数和岗位名，不再逐条重复「完成」', async () => {
    const user = userEvent.setup()
    const names: Record<string, string> = { j1: 'Java 后端', j2: 'AI 应用开发' }
    renderWithProviders(<ToolSteps steps={READS.slice(0, 5)} title={(id) => names[id]} />)

    await user.click(screen.getByRole('button', { name: /查看岗位库/ }))
    expect(screen.getAllByText('× 2', { selector: '.tool-step-count' })).toHaveLength(2)
    expect(screen.getByText('Java 后端、AI 应用开发')).toBeInTheDocument()
    expect(screen.queryByText('完成')).not.toBeInTheDocument()
  })
})
