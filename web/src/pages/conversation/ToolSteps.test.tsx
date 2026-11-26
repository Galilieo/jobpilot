import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
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
