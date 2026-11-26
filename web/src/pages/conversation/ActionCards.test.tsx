import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AgentActionCard } from '../../api/types'
import { renderWithProviders } from '../../test/render'
import { PendingCard, SKIPPED_ANSWER } from './ActionCards'

const base = { action_id: 'a1', run_id: 'r1', status: 'pending' as const }
const title = () => '后端实习生'

describe('确认卡', () => {
  it('确认按钮点一次就禁用，连点也只提交一次', async () => {
    const user = userEvent.setup()
    let finish: (value: string | null) => void = () => undefined
    const decide = vi.fn(() => new Promise<string | null>((resolve) => (finish = resolve)))
    const card: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'set_current_job',
      args: { job_id: 'j1', title: '后端实习生' },
    }
    renderWithProviders(<PendingCard card={card} title={title} decide={decide} />)

    expect(screen.getByText('把当前岗位设为「后端实习生」')).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: '确认切换' })
    await user.click(confirm)
    await user.click(confirm)
    expect(decide).toHaveBeenCalledTimes(1)
    expect(decide).toHaveBeenCalledWith({ decision: 'confirm' })
    expect(screen.getByRole('button', { name: /拒\s?绝/ })).toBeDisabled()
    finish(null)
  })

  it('修改简历条目显示改前、改后和依据检查结论', () => {
    const card: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'edit_resume_item',
      args: {
        previous_text: '负责接口开发',
        text: '主导接口开发',
        check: {
          text: '负责接口开发与联调',
          layers: 'full',
          issues: [{ kind: 'role', message: '「主导」超出你确认的职责程度', fragment: null }],
        },
      },
    }
    const { container } = renderWithProviders(
      <PendingCard card={card} title={title} decide={vi.fn()} />,
    )

    expect(container.querySelector('.action-card.is-pending')).toBeInTheDocument()
    expect(screen.getByText('等你确认')).toBeInTheDocument()
    expect(screen.getByText('现在')).toBeInTheDocument()
    expect(screen.getByText('改成')).toBeInTheDocument()
    expect(screen.getByText('负责接口开发')).toBeInTheDocument()
    expect(screen.getByText('负责接口开发与联调')).toBeInTheDocument()
    expect(screen.getByText('「主导」超出你确认的职责程度')).toBeInTheDocument()
  })

  it('不认识的工具用通用渲染：工具名和参数键值对，仍可确认', async () => {
    const user = userEvent.setup()
    const decide = vi.fn(async () => null)
    const card: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'save_memory',
      args: { kind: 'writing', text: '简历不用第一人称', tags: ['风格'] },
    }
    renderWithProviders(<PendingCard card={card} title={title} decide={decide} />)

    expect(screen.getByText('执行「save_memory」')).toBeInTheDocument()
    expect(screen.getByText('简历不用第一人称')).toBeInTheDocument()
    expect(screen.getByText('["风格"]')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '确认执行' }))
    expect(decide).toHaveBeenCalledWith({ decision: 'confirm' })
  })
})

describe('提问卡', () => {
  it('答案按问题 ID 一一对应，跳过的问题也交一个答案', async () => {
    const user = userEvent.setup()
    const decide = vi.fn(async () => null)
    const card: AgentActionCard = {
      ...base,
      kind: 'question',
      tool: 'ask_user',
      args: {
        type: 'questions',
        questions: [
          { question_id: 'q1', text: '你在项目里负责哪部分？' },
          { question_id: 'q2', text: '有量化结果吗？' },
        ],
      },
    }
    renderWithProviders(<PendingCard card={card} title={title} decide={decide} />)

    const submit = screen.getByRole('button', { name: '提交回答' })
    await user.type(screen.getByLabelText('你在项目里负责哪部分？'), '  后端接口 ')
    expect(submit).toBeDisabled() // 第二问还没答
    await user.click(screen.getAllByRole('button', { name: '先跳过' })[1])
    await user.click(submit)

    expect(decide).toHaveBeenCalledWith({
      decision: 'confirm',
      answers: { q1: '后端接口', q2: SKIPPED_ANSWER },
    })
  })

  it('岗位选择卡只带 job_choice', async () => {
    const user = userEvent.setup()
    const decide = vi.fn(async () => null)
    const card: AgentActionCard = {
      ...base,
      kind: 'question',
      tool: 'ask_user',
      args: { type: 'job_choice', question: '?', job_id: 'j2', choices: ['new', 'switch', 'keep'] },
    }
    renderWithProviders(<PendingCard card={card} title={title} decide={decide} />)

    await user.click(screen.getByRole('button', { name: '切换到新岗位' }))
    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith({ decision: 'confirm', job_choice: 'switch' }),
    )
  })
})
