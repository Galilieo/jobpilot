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

describe('记忆确认卡 remember / forget', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('remember(add) 显示栏目、增加动作、完整值并高亮新增项，以及原话来源', async () => {
    const user = userEvent.setup()
    const decide = vi.fn(async () => null)
    stubFetch({
      'GET /api/memories': () =>
        jsonResponse(200, [
          {
            key: 'cities',
            label: '城市',
            group: 'job',
            type: 'list',
            options: [],
            value: ['北京'],
            revision: 1,
            deleted: false,
          },
        ]),
    })
    const card: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'remember',
      args: {
        key: 'cities',
        label: '城市',
        op: 'add',
        value: ['北京', '上海'],
        quote: '也考虑上海',
        expected_revision: 1,
        source: { kind: 'conversation' },
      },
    }
    const { container } = renderWithProviders(
      <PendingCard card={card} title={title} decide={decide} />,
    )

    expect(screen.queryByText(/执行「/)).not.toBeInTheDocument()
    expect(screen.getByText('记住偏好')).toBeInTheDocument()
    expect(screen.getByText('城市')).toBeInTheDocument()
    expect(screen.getByText('增加')).toBeInTheDocument()
    expect(screen.getByText('北京')).toBeInTheDocument()
    expect(screen.getByText('上海')).toBeInTheDocument()
    expect(screen.getByText('来源：你刚才说的『也考虑上海』')).toBeInTheDocument()
    await waitFor(() => {
      const added = Array.from(container.querySelectorAll('.memory-chip.is-added')).map(
        (el) => el.textContent,
      )
      expect(added).toEqual(['上海'])
    })

    await user.click(screen.getByRole('button', { name: '确认记住' }))
    expect(decide).toHaveBeenCalledWith({ decision: 'confirm' })
  })

  it('remember(replace) 与 forget(remove / clear) 渲染动作、完整值和来源，并支持撤销结果行', async () => {
    const user = userEvent.setup()
    const replaceCard: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'remember',
      args: {
        key: 'tone',
        label: '语气',
        op: 'replace',
        value: '偏技术细节',
        quote: '以后写简历偏技术细节一点',
        expected_revision: 1,
        source: { kind: 'conversation' },
      },
    }
    const { unmount } = renderWithProviders(
      <PendingCard card={replaceCard} title={title} decide={vi.fn()} />,
    )
    expect(screen.getByText('替换')).toBeInTheDocument()
    expect(screen.getByText('偏技术细节')).toBeInTheDocument()
    expect(screen.getByText('来源：你刚才说的『以后写简历偏技术细节一点』')).toBeInTheDocument()
    unmount()

    const forgetCard: AgentActionCard = {
      ...base,
      kind: 'confirmation',
      tool: 'forget',
      args: {
        key: 'job_notes',
        label: '其他求职偏好',
        op: 'clear',
        value: null,
        quote: '忘掉其他求职偏好',
        expected_revision: 1,
        source: { kind: 'conversation' },
      },
    }
    renderWithProviders(<PendingCard card={forgetCard} title={title} decide={vi.fn()} />)
    expect(screen.getByText('忘掉偏好')).toBeInTheDocument()
    expect(screen.getByText('清空')).toBeInTheDocument()
    expect(screen.getByText('来源：你刚才说的『忘掉其他求职偏好』')).toBeInTheDocument()

    const onUndo = vi.fn()
    renderWithProviders(
      <CardResult
        tool="remember"
        title={title}
        outcome={{
          state: 'decided',
          action: {
            action_id: 'a2',
            run_id: 'r1',
            kind: 'confirmation',
            tool: 'remember',
            args: {},
            status: 'executed',
            result: { key: 'cities', op: 'add', revision: 2 },
          },
        }}
        onUndo={onUndo}
      />,
    )
    await user.click(screen.getByRole('button', { name: /撤\s?销/ }))
    expect(onUndo).toHaveBeenCalledTimes(1)
  })
})
