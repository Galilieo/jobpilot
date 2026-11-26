// 一串工具调用折叠成一行进度（「✓ 已完成 4 步」），展开看每一步的工具名和简短结果。
import { ThoughtChain } from '@ant-design/x'
import type { ThoughtChainItemType } from '@ant-design/x'
import type { ReactElement } from 'react'
import { STOP_REASON, toolLabel } from '../../labels'

export interface StepView {
  key: string
  tool: string
  status: 'loading' | 'success' | 'error'
  result: Record<string, unknown> | null
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

function headline(steps: StepView[]): { title: string; status: ThoughtChainItemType['status'] } {
  const running = steps.find((step) => step.status === 'loading')
  if (running) return { title: `正在${toolLabel(running.tool)}…`, status: 'loading' }
  const failed = steps.filter((step) => step.status === 'error').length
  const done = `已完成 ${steps.length} 步`
  return failed > 0
    ? { title: `${done}，${failed} 步没成功`, status: 'error' }
    : { title: done, status: 'success' }
}

export function ToolSteps({ steps }: { steps: StepView[] }): ReactElement {
  const { title, status } = headline(steps)
  return (
    <ThoughtChain
      className="tool-steps"
      items={[
        {
          key: 'steps',
          title,
          status,
          collapsible: true,
          blink: status === 'loading',
          content: (
            <ol className="tool-step-list">
              {steps.map((step) => (
                <li key={step.key} className={step.status === 'error' ? 'is-error' : undefined}>
                  <span className="tool-step-name">{toolLabel(step.tool)}</span>
                  <span className="tool-step-result">{stepSummary(step)}</span>
                </li>
              ))}
            </ol>
          ),
        },
      ]}
    />
  )
}
