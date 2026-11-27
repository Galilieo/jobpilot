// 工具步骤的整理：每步的简短结果，以及时间轴里相邻同一工具的合并。纯函数，不渲染。
import { STOP_REASON } from '../../labels'

export type JobTitle = (jobId: string) => string

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

export interface StepGroup {
  key: string
  tool: string
  status: StepView['status']
  count: number
  jobIds: string[]
  summary: string
  durationMs?: number
}

function jobIdOf(step: StepView): string | null {
  const jobId = step.result?.job_id
  return typeof jobId === 'string' ? jobId : null
}

/** 相邻、同一工具、都成功的步骤合成一组；出错和进行中的单独成行，原因才不会被盖住。 */
export function groupSteps(steps: StepView[]): StepGroup[] {
  const groups: StepGroup[] = []
  for (const step of steps) {
    const last = groups.at(-1)
    const jobId = jobIdOf(step)
    const summary = stepSummary(step)
    const mergeable =
      last?.tool === step.tool && step.status === 'success' && last.summary === summary
    if (last && mergeable) {
      last.count += 1
      if (jobId && !last.jobIds.includes(jobId)) last.jobIds.push(jobId)
      if (step.durationMs !== undefined) last.durationMs = (last.durationMs ?? 0) + step.durationMs
      continue
    }
    groups.push({
      key: step.key,
      tool: step.tool,
      status: step.status,
      count: 1,
      jobIds: jobId ? [jobId] : [],
      summary,
      ...(step.durationMs !== undefined && { durationMs: step.durationMs }),
    })
  }
  return groups
}

export function groupDetail(group: StepGroup, title?: JobTitle): string | null {
  if (group.status !== 'success') return group.summary
  if (group.jobIds.length > 0 && title) return group.jobIds.map(title).join('、')
  return group.summary === '完成' ? null : group.summary
}
