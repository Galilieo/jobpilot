// 确认卡、提问卡和卡片决定后的一行结果。卡片处于哪种状态由调用方按详情判断后传进来。
// 写入确认按工具分别渲染，认不出的工具用通用的键值对兜底，以后新加的工具也能确认。
import { CheckOutlined, ExclamationCircleFilled, LoadingOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { App, Button, Input, Space, Typography } from 'antd'
import { useMemo, useState, type ReactElement, type ReactNode } from 'react'
import { Link } from 'react-router'
import { tailoring } from '../../api/endpoints'
import type {
  AgentActionCard,
  AgentDecidedAction,
  AgentDecision,
  AgentJobChoice,
  AgentQuestionCardArgs,
  BulletCheck,
} from '../../api/types'
import { FactBadge } from '../../components/FactBadge'
import { indexResumeFacts, type IndexedFact } from '../../factIndex'
import { toolLabel } from '../../labels'
import { tailorResumeQuery } from '../../queryKeys'
import type { ToolResultStep } from './timeline'

type Decide = (decision: AgentDecision) => Promise<string | null>
type JobTitle = (jobId: string) => string

// 「先跳过」也要按问题交一个答案（后端要求答案和问题一一对应、不能为空）。
export const SKIPPED_ANSWER = '先跳过'

const JOB_CHOICE_LABEL: Record<AgentJobChoice, string> = {
  new: '新开对话',
  switch: '切换到新岗位',
  keep: '保持不变',
}

/** 点下后立即禁用（React 对点击同步更新），失败才恢复；同一张卡不会因为连点提交两次。 */
function useOnceSubmit(decide: Decide) {
  const { message } = App.useApp()
  const [submitting, setSubmitting] = useState<string | null>(null)
  async function submit(key: string, decision: AgentDecision): Promise<void> {
    setSubmitting(key)
    const failure = await decide(decision)
    if (failure) {
      void message.error(failure)
      setSubmitting(null)
    }
  }
  return { submitting, submit }
}

function CardFrame({
  kind,
  subtitle,
  children,
}: {
  kind: string
  subtitle?: string
  children: ReactNode
}): ReactElement {
  return (
    <section className="action-card is-pending" aria-label={kind}>
      <div className="action-card-head">
        <strong>{kind}</strong>
        {subtitle && <span className="action-card-sub">{subtitle}</span>}
        <span className="action-card-state">等你确认</span>
      </div>
      <div className="action-card-body">{children}</div>
    </section>
  )
}

function GroundingVerdict({
  check,
  facts = [],
}: {
  check: BulletCheck
  facts?: IndexedFact[]
}): ReactElement {
  if (check.issues.length === 0) {
    return (
      <div className="grounding-verdict is-ok">
        <CheckOutlined className="grounding-verdict-icon" />
        <span>
          依据检查通过：每处说法都有你确认过的事实
          {facts.map((item) => (
            <FactBadge
              key={item.factId}
              code={item.code}
              text={item.fact.text}
              variant="on-green"
            />
          ))}
        </span>
      </div>
    )
  }
  return (
    <div className="grounding-verdict is-warning">
      <Typography.Text type="warning">
        <ExclamationCircleFilled /> 依据检查发现 {check.issues.length} 处问题
      </Typography.Text>
      <ul>
        {check.issues.map((issue, index) => (
          <li key={index}>{issue.message}</li>
        ))}
      </ul>
    </div>
  )
}

function ResumeEditBody({ args }: { args: Record<string, unknown> }): ReactElement {
  const check = args.check as BulletCheck | undefined
  const runId = typeof args.run_id === 'string' ? args.run_id : ''
  const bulletId = typeof args.bullet_id === 'string' ? args.bullet_id : ''
  const resume = useQuery({
    queryKey: tailorResumeQuery(runId),
    queryFn: ({ signal }) => tailoring.resume(runId, undefined, signal),
    enabled: Boolean(runId && bulletId),
  })
  const facts = useMemo(() => {
    if (!resume.data || !bulletId) return []
    return indexResumeFacts(resume.data).bulletFacts(bulletId)
  }, [bulletId, resume.data])

  return (
    <>
      <div className="before-after">
        <div>
          <small>现在</small>
          <del>{String(args.previous_text ?? '')}</del>
        </div>
        <div className="is-after">
          <small>改成</small>
          <p>{check?.text ?? String(args.text ?? '')}</p>
        </div>
      </div>
      {check && <GroundingVerdict check={check} facts={facts} />}
    </>
  )
}

function GenericBody({ card }: { card: AgentActionCard }): ReactElement {
  return (
    <dl className="generic-args">
      {Object.entries(card.args).map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function confirmationView(card: AgentActionCard, title: JobTitle) {
  const args = card.args as Record<string, unknown>
  if (card.tool === 'set_current_job') {
    const jobId = typeof args.job_id === 'string' ? args.job_id : null
    const name = typeof args.title === 'string' ? args.title : jobId && title(jobId)
    return {
      kind: '切换当前岗位',
      body: (
        <div className="action-card-content">
          <p>{jobId ? `把当前岗位设为「${name}」` : '解除这段对话的当前岗位'}</p>
        </div>
      ),
      confirm: jobId ? '确认切换' : '确认解除',
      reject: '拒绝',
    }
  }
  if (card.tool === 'edit_resume_item') {
    return {
      kind: '修改简历条目',
      body: <ResumeEditBody args={args} />,
      confirm: '确认修改',
      reject: '不改了',
    }
  }
  return {
    kind: `执行「${toolLabel(card.tool)}」`,
    body: (
      <div className="action-card-content">
        <GenericBody card={card} />
      </div>
    ),
    confirm: '确认执行',
    reject: '拒绝',
  }
}

function ConfirmationCard(props: {
  card: AgentActionCard
  title: JobTitle
  decide: Decide
}): ReactElement {
  const { submitting, submit } = useOnceSubmit(props.decide)
  const view = confirmationView(props.card, props.title)
  return (
    <CardFrame kind={view.kind}>
      {view.body}
      <div className="action-card-actions">
        <Button
          type="text"
          disabled={submitting !== null}
          loading={submitting === 'reject'}
          onClick={() => void submit('reject', { decision: 'reject' })}
        >
          {view.reject}
        </Button>
        <Button
          type="primary"
          disabled={submitting !== null}
          loading={submitting === 'confirm'}
          onClick={() => void submit('confirm', { decision: 'confirm' })}
        >
          {view.confirm}
        </Button>
      </div>
    </CardFrame>
  )
}

function QuestionsCard(props: {
  questions: Extract<AgentQuestionCardArgs, { type: 'questions' }>['questions']
  decide: Decide
}): ReactElement {
  const { submitting, submit } = useOnceSubmit(props.decide)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const complete = props.questions.every((question) => answers[question.question_id]?.trim())

  function answer(questionId: string, value: string): void {
    setAnswers((current) => ({ ...current, [questionId]: value }))
  }

  return (
    <CardFrame kind="需要你补充">
      <div className="action-card-content action-card-questions">
        {props.questions.map((question) => {
          const value = answers[question.question_id] ?? ''
          const skipped = value === SKIPPED_ANSWER
          return (
            <div key={question.question_id} className="question-row">
              <Typography.Text>{question.text}</Typography.Text>
              <Input.TextArea
                aria-label={question.text}
                value={skipped ? '' : value}
                placeholder={skipped ? '已跳过这一问' : '写下你的回答'}
                disabled={skipped || submitting !== null}
                autoSize={{ minRows: 1, maxRows: 4 }}
                maxLength={2000}
                onChange={(event) => answer(question.question_id, event.target.value)}
              />
              <Button
                type="link"
                size="small"
                disabled={submitting !== null}
                onClick={() => answer(question.question_id, skipped ? '' : SKIPPED_ANSWER)}
              >
                {skipped ? '改为回答' : '先跳过'}
              </Button>
            </div>
          )
        })}
      </div>
      <div className="action-card-actions">
        <Button
          type="primary"
          disabled={!complete || submitting !== null}
          loading={submitting === 'confirm'}
          onClick={() => {
            const trimmed = Object.fromEntries(
              props.questions.map((q) => [q.question_id, answers[q.question_id].trim()]),
            )
            void submit('confirm', { decision: 'confirm', answers: trimmed })
          }}
        >
          提交回答
        </Button>
      </div>
    </CardFrame>
  )
}

function JobChoiceCard(props: {
  args: Extract<AgentQuestionCardArgs, { type: 'job_choice' }>
  title: JobTitle
  decide: Decide
}): ReactElement {
  const { submitting, submit } = useOnceSubmit(props.decide)
  return (
    <CardFrame kind="又收到一个岗位">
      <div className="action-card-content">
        <p>
          新岗位「{props.title(props.args.job_id)}」已存入岗位库。要怎么处理这段对话的当前岗位？
        </p>
      </div>
      <Space className="action-card-actions" wrap>
        {props.args.choices.map((choice, index) => (
          <Button
            key={choice}
            type={index === 0 ? 'primary' : 'default'}
            disabled={submitting !== null}
            loading={submitting === choice}
            onClick={() => void submit(choice, { decision: 'confirm', job_choice: choice })}
          >
            {JOB_CHOICE_LABEL[choice]}
          </Button>
        ))}
      </Space>
    </CardFrame>
  )
}

export function PendingCard(props: {
  card: AgentActionCard
  title: JobTitle
  decide: Decide
}): ReactElement {
  const { card } = props
  if (card.kind === 'question') {
    return card.args.type === 'job_choice' ? (
      <JobChoiceCard args={card.args} title={props.title} decide={props.decide} />
    ) : (
      <QuestionsCard questions={card.args.questions} decide={props.decide} />
    )
  }
  return <ConfirmationCard card={card} title={props.title} decide={props.decide} />
}

// ---------- 决定之后 ----------

const UNDOABLE_TOOLS = new Set(['set_current_job', 'edit_resume_item'])

function canUndo(action: AgentDecidedAction): boolean {
  if (action.status !== 'executed') return false
  // 提问卡只有「切换到新岗位」改了绑定，能撤销。
  return UNDOABLE_TOOLS.has(action.tool) || action.result?.decision === 'switch'
}

/** 已执行写入的结果文字。result 是卡片的执行结果或卡后那一步工具的结果摘要。 */
function executedText(tool: string, result: Record<string, unknown>, title: JobTitle): ReactNode {
  const jobId = typeof result.job_id === 'string' ? result.job_id : null
  if (tool === 'set_current_job')
    return jobId ? `已把当前岗位设为「${title(jobId)}」` : '已解除当前岗位'
  if (tool === 'edit_resume_item') return '已修改这条简历条目'
  if (tool === 'ask_user') {
    if (result.decision === 'switch') return `已切换到新岗位「${jobId && title(jobId)}」`
    if (result.decision === 'keep') return '保持原来的当前岗位'
    if (result.decision === 'new' && typeof result.conversation_id === 'string') {
      return (
        <>
          已为新岗位新开对话，<Link to={`/conversations/${result.conversation_id}`}>去新对话</Link>
        </>
      )
    }
    return '已收到你的回答'
  }
  return `已执行「${toolLabel(tool)}」`
}

export type CardOutcome =
  | { state: 'saving' }
  | { state: 'decided'; action: AgentDecidedAction }
  | { state: 'past'; step: ToolResultStep | null }

function outcomeText(tool: string, outcome: CardOutcome, title: JobTitle): ReactNode {
  if (outcome.state === 'saving') return '已提交你的决定，正在执行…'
  if (outcome.state === 'past') {
    const step = outcome.step
    if (!step) return '这张卡已失效'
    if (step.decision === 'rejected') return '你拒绝了这项操作'
    if (typeof step.error === 'string') return `没有执行：${step.error}`
    return executedText(tool, step, title)
  }
  const { action } = outcome
  const result = action.result ?? {}
  switch (action.status) {
    case 'rejected':
      return '你拒绝了这项操作'
    case 'failed':
      return `没有执行：${typeof result.error === 'string' ? result.error : '执行时出错'}`
    case 'undone':
      return '已撤销'
    case 'confirmed':
      return '已确认，正在执行…'
    default:
      return executedText(action.tool, result, title)
  }
}

export function CardResult(props: {
  tool: string
  outcome: CardOutcome
  title: JobTitle
  onUndo?: () => void
  undoing?: boolean
}): ReactElement {
  const { outcome } = props
  const busy =
    outcome.state === 'saving' ||
    (outcome.state === 'decided' && outcome.action.status === 'confirmed')
  const undoable = outcome.state === 'decided' && canUndo(outcome.action) && props.onUndo
  return (
    <div className="card-result">
      {busy ? (
        <LoadingOutlined />
      ) : (
        <CheckOutlined className="card-result-icon" aria-label={toolLabel(props.tool)} />
      )}
      <Typography.Text type="secondary">
        {outcomeText(props.tool, outcome, props.title)}
      </Typography.Text>
      {undoable && (
        <Button
          type="link"
          size="small"
          className="card-result-undo"
          loading={props.undoing}
          onClick={props.onUndo}
        >
          撤销
        </Button>
      )}
    </div>
  )
}
