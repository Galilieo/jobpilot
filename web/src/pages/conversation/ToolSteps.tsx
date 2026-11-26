// 一轮里的多个工具调用合并成一行进度（「解析岗位、匹配、生成简历 3 步，用时 21 秒」），点开是时间轴。
import { ThoughtChain } from '@ant-design/x'
import type { ThoughtChainItemType } from '@ant-design/x'
import { useState, type ReactElement } from 'react'
import { STOP_REASON, toolLabel } from '../../labels'
import { formatStepDuration, formatStepsSummary, formatTotalDuration, toolAction } from './naming'

export interface StepView {
  key: string
  tool: string
  status: 'loading' | 'success' | 'error'
  result: Record<string, unknown> | null
  durationMs?: number
}

/** 一步的简短结果：出错就说原因，其他只给计数或「完成」，不把原始 JSON 摆出来。 */
function stepSummary(step: StepView): string {
  const result = step.result ?? {}
  if (step.status === 'loading') return '进行中'
  if (typeof result.error === 'string') return result.error
  if (result.status === 'budget_exhausted') return '本轮额度用完'
  if (typeof result.stop_reason === 'string') return STOP_REASON[result.stop_reason] ?? '部分完成'
  if (typeof result.total === 'number') return `共 ${result.total} 项`
  if (result.decision === 'rejected') return '你拒绝了'
  return '完成'
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

export function ToolSteps({ steps }: { steps: StepView[] }): ReactElement {
  const [open, setOpen] = useState(false)
  const status = headlineStatus(steps)
  const running = steps.find((step) => step.status === 'loading')
  const failed = steps.filter((step) => step.status === 'error').length
  const totalMs = totalDurationMs(steps)
  const actionsText = formatStepsSummary(steps.map((step) => step.tool))

  const title = running ? (
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
          title,
          status,
          collapsible: true,
          blink: status === 'loading',
          content: (
            <ol className="tool-step-timeline">
              {steps.map((step) => {
                const summary = stepSummary(step)
                const showSummary =
                  step.status === 'error' ||
                  step.status === 'loading' ||
                  summary !== '完成' ||
                  step.durationMs === undefined
                return (
                  <li key={step.key} className={step.status === 'error' ? 'is-error' : undefined}>
                    <span className="tool-step-name">{toolLabel(step.tool)}</span>
                    {showSummary && <span className="tool-step-result">{summary}</span>}
                    {step.durationMs !== undefined && (
                      <time className="tool-step-time tabular-num">
                        {formatStepDuration(step.durationMs)}
                      </time>
                    )}
                  </li>
                )
              })}
            </ol>
          ),
        },
      ]}
    />
  )
}
