// 对话里要显示的名字：新对话的标题、岗位 ID 对应的岗位名、工具步骤的动作短语与耗时、token 数的格式。
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
  set_writing_preference: '设置写作偏好',
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
  search_conversations: '搜索其他对话',
}

// 收起时概括用。读取类只写读了什么（read_job 按岗位个数另算），其余写完成后的短语。
const READ_OBJECT: Record<string, string> = {
  read_facts: '你的经历',
  read_match_report: '匹配报告',
  set_writing_preference: '写作偏好',
  read_resume: '定制简历',
  read_conversation_history: '更早的对话',
}

const DONE_PHRASE: Record<string, string> = {
  list_jobs: '查看了岗位库',
  parse_jd: '解析了 JD',
  match_job: '做了匹配',
  tailor_resume: '生成了定制简历',
  set_current_job: '切换了当前岗位',
  edit_resume_item: '改了简历条目',
  ask_user: '向你提了问题',
  record_application: '记了投递',
  remember: '记下了偏好',
  forget: '删掉了偏好',
  search_conversations: '搜索了其他对话',
}

export function readObject(tool: string): string | null {
  return READ_OBJECT[tool] ?? null
}

export function donePhrase(tool: string): string {
  return DONE_PHRASE[tool] ?? toolLabel(tool)
}

export function toolAction(tool: string): string {
  return TOOL_ACTION[tool] ?? toolLabel(tool)
}

export function formatTotalDuration(ms: number): string {
  return ms >= 1000 ? `${Math.round(ms / 1000)} 秒` : `${(ms / 1000).toFixed(1)} 秒`
}

export function formatTokens(tokens: number): string {
  return tokens >= 10_000 ? `${(tokens / 10_000).toFixed(1)} 万` : String(tokens)
}

export function titleFrom(input: LiveInput): string {
  const firstLine = input.content.trim().split('\n')[0]?.trim()
  const name = firstLine || input.attachments[0]?.name || '新对话'
  return name.length > TITLE_LENGTH ? `${name.slice(0, TITLE_LENGTH)}…` : name
}

export function useJobTitle(): (jobId: string) => string {
  const list = useQuery({
    queryKey: JOBS_QUERY,
    queryFn: ({ signal }) => jobs.list(undefined, signal),
  })
  return (jobId) => list.data?.find((job) => job.job_id === jobId)?.title ?? '未命名岗位'
}
