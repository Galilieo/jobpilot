// 工具步骤的整理：每步的简短结果、按动作分组、收起时的一句话概括，以及展开后每次调用的细节。
// 细节来自后端步骤记录的 detail（岗位名、事实库版本和条数），纯函数，不渲染。
import { STOP_REASON } from '../../labels'
import { messageAnchor } from './messageLink'
import { donePhrase, readObject } from './naming'

export type JobTitle = (jobId: string) => string

export interface StepView {
  key: string
  tool: string
  status: 'loading' | 'success' | 'error'
  result: Record<string, unknown> | null
  durationMs?: number
}

type Detail = Record<string, unknown>

export interface StepCall {
  jobId: string | null
  detail: Detail | null
}

export interface StepGroup {
  key: string
  tool: string
  status: StepView['status']
  count: number
  jobIds: string[]
  calls: StepCall[]
  summary: string
  durationMs?: number
}

export interface DetailLine {
  key: string
  text: string
  jobId?: string
  messageHref?: string
  snippet?: string
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

function callOf(step: StepView): StepCall {
  const detail = step.result?.detail
  return {
    jobId: jobIdOf(step),
    detail: detail && typeof detail === 'object' ? (detail as Detail) : null,
  }
}

function newGroup(step: StepView): StepGroup {
  const jobId = jobIdOf(step)
  return {
    key: step.key,
    tool: step.tool,
    status: step.status,
    count: 1,
    jobIds: jobId ? [jobId] : [],
    calls: [callOf(step)],
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
    same.calls.push(callOf(step))
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

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function count(value: unknown): number {
  return typeof value === 'number' ? value : 0
}

function jobText(detail: Detail): string {
  const parts = [text(detail.title), text(detail.company), text(detail.location)]
  return parts.filter(Boolean).join(' · ') || '未命名岗位'
}

function jobLines(group: StepGroup): DetailLine[] {
  const seen = new Set<string>()
  const lines: DetailLine[] = []
  const listed = group.calls.flatMap((call) =>
    Array.isArray(call.detail?.jobs) ? (call.detail.jobs as Detail[]) : [],
  )
  const read = group.calls.flatMap((call) =>
    call.detail ? [{ ...call.detail, job_id: call.jobId }] : [],
  )
  for (const job of group.tool === 'list_jobs' ? listed : read) {
    const jobId = text(job.job_id)
    if (jobId && seen.has(jobId)) continue
    if (jobId) seen.add(jobId)
    lines.push({ key: jobId ?? String(lines.length), text: jobText(job), ...(jobId && { jobId }) })
  }
  return lines
}

function factsLine(detail: Detail): string {
  const parts = [`第 ${count(detail.version)} 版`, `${count(detail.facts)} 条事实`]
  if (count(detail.criteria) > 0) parts.push(`${count(detail.criteria)} 条求职条件`)
  if (count(detail.education) > 0) parts.push(`${count(detail.education)} 段教育`)
  return parts.join(' · ')
}

function reportLines(detail: Detail): DetailLine[] {
  const gaps = Array.isArray(detail.main_gaps) ? detail.main_gaps.filter((gap) => text(gap)) : []
  return [
    { key: 'items', text: `${count(detail.items)} 项要求逐条对照` },
    ...gaps.map((gap, index) => ({ key: `gap:${index}`, text: `主要差距：${gap}` })),
  ]
}

function searchLines({ detail }: StepCall, index: number): DetailLine[] {
  if (!detail || typeof detail.query !== 'string') return []
  const hits = Array.isArray(detail.hits) ? (detail.hits as Detail[]) : []
  const summary = detail.degraded
    ? ' · 结果可能不全'
    : detail.index_pending
      ? ' · 部分新消息仍在建立索引'
      : ''
  const lines: DetailLine[] = [
    { key: `${index}:query`, text: `「${detail.query}」 · ${hits.length} 条命中${summary}` },
  ]
  for (const [rank, hit] of hits.entries()) {
    if (hit.source_deleted === true) {
      lines.push({
        key: `${index}:hit:${rank}`,
        text: '来源已删除',
      })
      continue
    }
    const id = text(hit.conversation_id)
    const seq = hit.seq
    if (!id || typeof seq !== 'number') continue
    lines.push({
      key: `${index}:hit:${rank}`,
      text: `${text(hit.title) ?? '未命名对话'} · 第 ${seq} 条消息`,
      messageHref: `/conversations/${encodeURIComponent(id)}#${messageAnchor(seq)}`,
      snippet: text(hit.snippet) ?? undefined,
    })
  }
  return lines
}

/** 展开一组后逐条列出：读过哪些岗位、事实库第几版多少条、报告的主要差距。没有细节的工具返回空。 */
export function detailLines(group: StepGroup): DetailLine[] {
  if (group.status !== 'success') return []
  const last = group.calls.at(-1)?.detail
  switch (group.tool) {
    case 'list_jobs':
    case 'read_job':
      return jobLines(group)
    case 'read_facts':
      return last ? [{ key: 'facts', text: factsLine(last) }] : []
    case 'read_match_report':
      return last ? reportLines(last) : []
    case 'read_resume':
      return last ? [{ key: 'resume', text: `第 ${count(last.version)} 版` }] : []
    case 'search_conversations':
      return group.calls.flatMap(searchLines)
    default:
      return []
  }
}
