// 一轮里的多个工具调用合并成一行进度（「解析岗位、匹配、生成简历 3 步，用时 21 秒」），点开是时间轴。
// 时间轴里相邻的同一工具再合成一行（「读取岗位详情 × 5」），写出读了哪些岗位，不逐条重复「完成」。
import { ThoughtChain } from '@ant-design/x'
import type { ThoughtChainItemType } from '@ant-design/x'
import { useState, type ReactElement } from 'react'
import { toolLabel } from '../../labels'
import { formatStepDuration, formatStepsSummary, formatTotalDuration, toolAction } from './naming'
import { groupDetail, groupSteps, type JobTitle, type StepView } from './stepGroups'

export type { StepView } from './stepGroups'

function StepTimeline({ steps, title }: { steps: StepView[]; title?: JobTitle }): ReactElement {
  return (
    <ol className="tool-step-timeline">
      {groupSteps(steps).map((group) => {
        const detail = groupDetail(group, title)
        return (
          <li key={group.key} className={group.status === 'error' ? 'is-error' : undefined}>
            <span className="tool-step-name">{toolLabel(group.tool)}</span>
            {group.count > 1 && (
              <span className="tool-step-count tabular-num">× {group.count}</span>
            )}
            {detail && <span className="tool-step-result">{detail}</span>}
            {group.durationMs !== undefined && (
              <time className="tool-step-time tabular-num">
                {formatStepDuration(group.durationMs)}
              </time>
            )}
          </li>
        )
      })}
    </ol>
  )
}

function totalDurationMs(steps: StepView[]): number | null {
  let total = 0
  let hasAny = false
  for (const step of steps) {
    if (typeof step.durationMs === 'number') {
      total += step.durationMs
      hasAny = true
    }
  }
  return hasAny ? total : null
}

function headlineStatus(steps: StepView[]): ThoughtChainItemType['status'] {
  if (steps.some((step) => step.status === 'loading')) return 'loading'
  if (steps.some((step) => step.status === 'error')) return 'error'
  return 'success'
}

export function ToolSteps({ steps, title }: { steps: StepView[]; title?: JobTitle }): ReactElement {
  const [open, setOpen] = useState(false)
  const status = headlineStatus(steps)
  const running = steps.find((step) => step.status === 'loading')
  const failed = steps.filter((step) => step.status === 'error').length
  const totalMs = totalDurationMs(steps)
  const actionsText = formatStepsSummary(steps.map((step) => step.tool))

  const heading = running ? (
    <button
      type="button"
      className="tool-steps-trigger"
      aria-expanded={open}
      onClick={(event) => {
        event.stopPropagation()
        setOpen((current) => !current)
      }}
    >
      <span className="tool-steps-actions">正在{toolAction(running.tool)}…</span>
    </button>
  ) : (
    <button
      type="button"
      className="tool-steps-trigger"
      aria-expanded={open}
      onClick={(event) => {
        event.stopPropagation()
        setOpen((current) => !current)
      }}
    >
      <span className="tool-steps-actions">{actionsText}</span>
      <span className="tool-steps-meta tabular-num">
        {steps.length} 步{totalMs !== null ? `，用时 ${formatTotalDuration(totalMs)}` : ''}
        {failed > 0 ? `，${failed} 步没成功` : ''}
      </span>
    </button>
  )

  return (
    <ThoughtChain
      className="tool-steps"
      expandedKeys={open ? ['steps'] : []}
      onExpand={(keys) => setOpen(keys.includes('steps'))}
      items={[
        {
          key: 'steps',
          title: heading,
          status,
          collapsible: true,
          blink: status === 'loading',
          content: <StepTimeline steps={steps} title={title} />,
        },
      ]}
    />
  )
}
