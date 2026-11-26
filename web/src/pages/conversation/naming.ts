// 对话里要显示的名字：新对话的标题、岗位 ID 对应的岗位名、工具步骤的动作短语与耗时格式。
import { useQuery } from '@tanstack/react-query'
import { jobs } from '../../api/endpoints'
import { toolLabel } from '../../labels'
import { JOBS_QUERY } from '../../queryKeys'
import type { LiveInput } from './runControl'

// 对话标题取第一条消息的开头，侧栏放得下。
const TITLE_LENGTH = 24

const TOOL_ACTION: Record<string, string> = {
  list_jobs: '查看岗位库',
  read_job: '读取岗位',
  read_facts: '读取事实',
  read_match_report: '读取匹配报告',
  read_resume: '读取简历',
  parse_jd: '解析岗位',
  match_job: '匹配',
  tailor_resume: '生成简历',
  set_current_job: '切换岗位',
  edit_resume_item: '修改条目',
  ask_user: '向你提问',
  remember: '记住偏好',
  forget: '忘掉偏好',
  read_conversation_history: '查阅对话',
}

export function toolAction(tool: string): string {
  return TOOL_ACTION[tool] ?? toolLabel(tool)
}

export function formatStepsSummary(tools: string[]): string {
  return [...new Set(tools.map(toolAction))].join('、')
}

export function formatStepDuration(ms: number): string {
  return `${(ms / 1000).toFixed(1)} 秒`
}

export function formatTotalDuration(ms: number): string {
  return ms >= 1000 ? `${Math.round(ms / 1000)} 秒` : `${(ms / 1000).toFixed(1)} 秒`
}

export function titleFrom(input: LiveInput): string {
  const firstLine = input.content.trim().split('\n')[0]?.trim()
  const name = firstLine || input.attachments[0]?.name || '新对话'
  return name.length > TITLE_LENGTH ? `${name.slice(0, TITLE_LENGTH)}…` : name
}

export function useJobTitle(): (jobId: string) => string {
  const list = useQuery({ queryKey: JOBS_QUERY, queryFn: ({ signal }) => jobs.list(signal) })
  return (jobId) => list.data?.find((job) => job.job_id === jobId)?.title ?? '未命名岗位'
}
