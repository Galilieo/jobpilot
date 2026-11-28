import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentActionCard, TailoredResumeData } from '../../api/types'
import { jsonResponse, stubFetch } from '../../test/fetchMock'
import { renderWithProviders } from '../../test/render'
import { CardResult, PendingCard, SKIPPED_ANSWER } from './ActionCards'

const base = { action_id: 'a1', run_id: 'r1', status: 'pending' as const }
const title = () => '后端实习生'

describe('确认卡', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })
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

  it('修改简历条目依据检查通过时在绿底上列出白底事实编号', async () => {
    const resume: TailoredResumeData = {
      resume_id: 'res-1',
      run_id: 'run-edit',
      version_id: 'v1',
      version: 1,
      current_version_id: 'v1',
      versions: [],
      content: {
        header: ['林同学'],
        style: { font: 'default', size_pt: 10.5, leading: 1.5 },
        sections: [
          {
            title: '项目经历',
            kind: 'project',
            entries: [
              {
                entry_id: 'e1',
                experience: '问答',
                heading: '问答',
                title: '问答',
                time: null,
                lines: [],
                bullets: [
                  {
                    bullet_id: 'bul-1',
                    text: '用 FastAPI 实现问答接口',
                    fact_ids: ['f-1', 'f-2'],
                    origin: 'passed',
                    edited: false,
                    check: { text: '用 FastAPI 实现问答接口', layers: 'full', issues: [] },
                  },
                ],
              },
            ],
          },
        ],
      },
      facts: {
        'f-1': {
          experience: '问答',
          text: '用 FastAPI 实现问答接口',
          source: 'resume',
          source_quote: null,
        },
        'f-2': {
          experience: '问答',
          text: '整理 48 条评测集',
          source: 'resume',
          source_quote: null,
        },
      },
    }
    stubFetch({
      'GET /api/tailor-runs/run-edit/resume': () => jsonResponse(200, resume),
    })

    const card: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'edit_resume_item',
      args: {
        run_id: 'run-edit',
        bullet_id: 'bul-1',
        previous_text: '实现问答接口',
        text: '用 FastAPI 实现问答接口',
        check: { text: '用 FastAPI 实现问答接口', layers: 'full', issues: [] },
      },
    }
    const { container } = renderWithProviders(
      <PendingCard card={card} title={title} decide={vi.fn()} />,
    )

    expect(await screen.findByText('F1')).toBeInTheDocument()
    expect(screen.getByText('F2')).toBeInTheDocument()
    expect(
      container.querySelector('.grounding-verdict.is-ok .fact-badge.is-on-green'),
    ).toBeInTheDocument()
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

it('升级前尚未决定的记忆卡可关闭', async () => {
  const user = userEvent.setup()
  const decide = vi.fn(async () => null)
  const card: AgentActionCard = {
    ...base,
    kind: 'confirmation',
    tool: 'remember',
    args: { key: 'excluded_types' },
  }
  renderWithProviders(<PendingCard card={card} title={title} decide={decide} />)
  expect(screen.getByText('旧版记忆确认')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: /关\s?闭/ }))
  expect(decide).toHaveBeenCalledWith({ decision: 'reject' })
})

it('新版记忆和遗忘确认卡标明记忆与来源原话，忘记显示待删原文', async () => {
  const user = userEvent.setup()
  const decide = vi.fn(async () => null)
  const rememberCard: AgentActionCard = {
    ...base,
    kind: 'confirmation',
    tool: 'remember',
    args: { category: 'job_pref', content: '不投外包公司', quote: '我不投外包' },
  }
  const forgetCard: AgentActionCard = {
    ...base,
    action_id: 'a2',
    kind: 'confirmation',
    tool: 'forget',
    args: {
      memory_id: 'm1',
      category: 'job_pref',
      content: '不投外包公司',
      quote: '外包也可以看看',
    },
  }
  renderWithProviders(
    <>
      <PendingCard card={rememberCard} title={title} decide={decide} />
      <PendingCard card={forgetCard} title={title} decide={decide} />
    </>,
  )
  expect(screen.getByText('记住记忆')).toBeInTheDocument()
  expect(screen.getByText('忘记记忆')).toBeInTheDocument()
  expect(screen.getAllByText('不投外包公司')).toHaveLength(2)
  expect(screen.getByText('你说的「我不投外包」')).toBeInTheDocument()
  expect(screen.getByText('你说的「外包也可以看看」')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '确认记住' }))
  expect(decide).toHaveBeenCalledWith({ decision: 'confirm' })
  await user.click(screen.getByRole('button', { name: '确认忘记' }))
  expect(decide).toHaveBeenCalledTimes(2)
})

it('shows the application details and offers undo after the confirmed write', async () => {
  const user = userEvent.setup()
  const decide = vi.fn(async () => null)
  const undo = vi.fn()
  const card: AgentActionCard = {
    ...base,
    kind: 'confirmation',
    tool: 'record_application',
    args: {
      job_id: 'j1',
      title: '平台实习',
      company: '示例公司',
      resume_version: 3,
      resume_generated_at: '2026-11-26T12:30:00',
      applied_at: '2026-11-27',
      channel: '官网',
    },
  }
  renderWithProviders(
    <>
      <PendingCard card={card} title={title} decide={decide} />
      <CardResult
        tool={card.tool}
        title={title}
        outcome={{
          state: 'decided',
          action: {
            ...card,
            status: 'executed',
            result: { application_id: 'submission', job_id: 'j1' },
          },
        }}
        onUndo={undo}
      />
    </>,
  )
  expect(screen.getByText('平台实习')).toBeInTheDocument()
  for (const detail of ['第 3 版简历', '示例公司', '2026-11-27', '官网']) {
    expect(screen.getByText(detail)).toBeInTheDocument()
  }
  expect(screen.getByText(/生成于.*11.*26.*12:30/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '确认记录' }))
  expect(decide).toHaveBeenCalledWith({ decision: 'confirm' })
  expect(screen.getByRole('link', { name: '查看岗位' })).toHaveAttribute('href', '/jobs/j1')
  expect(screen.getByRole('link', { name: '在投递页查看' })).toHaveAttribute(
    'href',
    '/applications?id=submission',
  )
  await user.click(screen.getByRole('button', { name: /撤\s?销/ }))
  expect(undo).toHaveBeenCalledOnce()

  renderWithProviders(
    <CardResult
      tool={card.tool}
      title={title}
      outcome={{ state: 'past', step: { tool: card.tool, job_id: 'j1' } }}
    />,
  )
  expect(screen.getAllByRole('link', { name: '在投递页查看' })[1]).toHaveAttribute(
    'href',
    '/applications',
  )
})
