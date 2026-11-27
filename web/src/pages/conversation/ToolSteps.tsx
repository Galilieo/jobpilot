// 一轮的工具步骤：运行中写出正在做什么，完成后收成一句话概括，点开按动作列出、读过的岗位做成标签；
// 有细节的动作还能再点开，逐条看读了哪些岗位、事实库第几版多少条。
// 参考 ChatGPT「思考了 N 秒」、Codex 正文间的灰色小字：默认收起，不逐条重复「完成」，也不摆时间轴。
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  LoadingOutlined,
  RightOutlined,
  ToolOutlined,
  UnorderedListOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useState, type ReactElement, type ReactNode } from 'react'
import { Link } from 'react-router'
import { toolLabel } from '../../labels'
import { formatTotalDuration, toolAction } from './naming'
import {
  describeSteps,
  detailLines,
  groupSteps,
  type DetailLine,
  type JobTitle,
  type StepGroup,
  type StepView,
} from './stepGroups'

export type { StepView } from './stepGroups'

// 展开后每行最多列两个岗位，其余写个数，一行放得下。
const MAX_JOB_TAGS = 2

const TOOL_ICON: Record<string, ReactNode> = {
  list_jobs: <UnorderedListOutlined />,
  read_facts: <UserOutlined />,
  read_job: <FileTextOutlined />,
}

function totalDurationMs(steps: StepView[]): number | null {
  const timed = steps.filter((step) => typeof step.durationMs === 'number')
  return timed.length > 0 ? timed.reduce((sum, step) => sum + (step.durationMs ?? 0), 0) : null
}

function JobTags({ jobIds, title }: { jobIds: string[]; title: JobTitle }): ReactElement {
  const rest = jobIds.length - MAX_JOB_TAGS
  return (
    <span className="tool-step-tags">
      {jobIds.slice(0, MAX_JOB_TAGS).map((jobId) => (
        <Link key={jobId} to={`/jobs/${jobId}`} className="tool-step-tag">
          {title(jobId)}
        </Link>
      ))}
      {rest > 0 && <span className="tool-step-tag is-more">+{rest}</span>}
    </span>
  )
}

function DetailList({ lines }: { lines: DetailLine[] }): ReactElement {
  return (
    <ul className="tool-step-details">
      {lines.map((line) => (
        <li key={line.key}>
          {line.jobId ? <Link to={`/jobs/${line.jobId}`}>{line.text}</Link> : line.text}
        </li>
      ))}
    </ul>
  )
}

function GroupRow({ group, title }: { group: StepGroup; title?: JobTitle }): ReactElement {
  const [open, setOpen] = useState(false)
  if (group.status === 'error') {
    return (
      <li className="tool-step is-error">
        <ExclamationCircleOutlined className="tool-step-icon" />
        <span>
          {toolLabel(group.tool)}没成功：{group.summary}
        </span>
      </li>
    )
  }
  const lines = detailLines(group)
  const head = (
    <>
      <span className="tool-step-icon">{TOOL_ICON[group.tool] ?? <ToolOutlined />}</span>
      <span className="tool-step-name">{toolLabel(group.tool)}</span>
      {group.summary !== '完成' && <span className="tool-step-meta">{group.summary}</span>}
    </>
  )
  if (lines.length === 0) {
    return (
      <li className="tool-step">
        {head}
        {title && group.jobIds.length > 0 && <JobTags jobIds={group.jobIds} title={title} />}
      </li>
    )
  }
  return (
    <li className="tool-step has-details">
      <button
        type="button"
        className="tool-step-toggle"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        {head}
        <RightOutlined className="tool-steps-chevron" aria-hidden />
      </button>
      {!open && title && group.jobIds.length > 0 && <JobTags jobIds={group.jobIds} title={title} />}
      {open && <DetailList lines={lines} />}
    </li>
  )
}

export function ToolSteps({ steps, title }: { steps: StepView[]; title?: JobTitle }): ReactElement {
  const [open, setOpen] = useState(false)
  const running = steps.find((step) => step.status === 'loading')
  if (running) {
    return (
      <div className="tool-steps is-running" role="status">
        <LoadingOutlined className="tool-steps-icon" />
        <span className="tool-steps-shimmer">正在{toolAction(running.tool)}…</span>
        <span className="tool-steps-meta tabular-num">第 {steps.indexOf(running) + 1} 步</span>
      </div>
    )
  }

  const groups = groupSteps(steps)
  const totalMs = totalDurationMs(steps)
  const failed = groups.some((group) => group.status === 'error')
  return (
    <div className="tool-steps">
      <button
        type="button"
        className="tool-steps-trigger"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        {failed ? (
          <ExclamationCircleOutlined className="tool-steps-icon is-error" />
        ) : (
          <CheckCircleOutlined className="tool-steps-icon" />
        )}
        <span className="tool-steps-summary">{describeSteps(groups)}</span>
        {totalMs !== null && (
          <span className="tool-steps-meta tabular-num">· {formatTotalDuration(totalMs)}</span>
        )}
        <RightOutlined className="tool-steps-chevron" aria-hidden />
      </button>
      {open && (
        <ul className="tool-step-list">
          {groups.map((group) => (
            <GroupRow key={group.key} group={group} title={title} />
          ))}
        </ul>
      )}
    </div>
  )
}
