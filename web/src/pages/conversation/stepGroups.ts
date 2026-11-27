// 工具步骤的整理：每步的简短结果、按动作分组，以及收起时的一句话概括。纯函数，不渲染。
import { STOP_REASON } from '../../labels'
import { donePhrase, readObject } from './naming'

export type JobTitle = (jobId: string) => string

export interface StepView {
  key: string
  tool: string
  status: 'loading' | 'success' | 'error'
  result: Record<string, unknown> | null
  durationMs?: number
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

function jobIdOf(step: StepView): string | null {
  const jobId = step.result?.job_id
  return typeof jobId === 'string' ? jobId : null
}

function newGroup(step: StepView): StepGroup {
  const jobId = jobIdOf(step)
  return {
    key: step.key,
    tool: step.tool,
    status: step.status,
    count: 1,
    jobIds: jobId ? [jobId] : [],
    summary: stepSummary(step),
    ...(step.durationMs !== undefined && { durationMs: step.durationMs }),
  }
}

/** 成功的步骤按动作合成一组（不必相邻），岗位去重；出错和进行中的各自成组，原因才不会被盖住。 */
export function groupSteps(steps: StepView[]): StepGroup[] {
  const groups: StepGroup[] = []
  for (const step of steps) {
    const same =
      step.status === 'success'
        ? groups.find((group) => group.tool === step.tool && group.status === 'success')
        : undefined
    if (!same) {
      groups.push(newGroup(step))
      continue
    }
    const jobId = jobIdOf(step)
    same.count += 1
    if (jobId && !same.jobIds.includes(jobId)) same.jobIds.push(jobId)
    const summary = stepSummary(step)
    if (summary !== '完成') same.summary = summary
    if (step.durationMs !== undefined) same.durationMs = (same.durationMs ?? 0) + step.durationMs
  }
  return groups
}

function joinObjects(objects: string[]): string {
  if (objects.length <= 1) return objects.join('')
  return `${objects.slice(0, -1).join('、')}和${objects.at(-1)}`
}

function objectOf(group: StepGroup): string | null {
  if (group.tool === 'read_job') return `${group.jobIds.length || group.count} 个岗位`
  return readObject(group.tool)
}

/** 收起时的概括：读取类合成「读了 A、B 和 C」放在第一次读取的位置，其余各用完成短语。 */
export function describeSteps(groups: StepGroup[]): string {
  const parts: string[] = []
  const reads: string[] = []
  let readAt = -1
  for (const group of groups.filter((item) => item.status === 'success')) {
    const object = objectOf(group)
    if (object === null) {
      parts.push(donePhrase(group.tool))
      continue
    }
    if (readAt < 0) readAt = parts.push('') - 1
    reads.push(object)
  }
  if (readAt >= 0) parts[readAt] = `读了${joinObjects(reads)}`
  const failed = groups.filter((group) => group.status === 'error').length
  if (failed > 0) parts.push(`${failed} 步没成功`)
  // 汉字和数字之间留空格：「读了 3 个岗位」「你的经历和 2 个岗位」。
  return parts.join('，').replace(/([\u4e00-\u9fff])(\d)/gu, '$1 $2')
}
