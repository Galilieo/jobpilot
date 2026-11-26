// 对话里要显示的名字：新对话的标题、岗位 ID 对应的岗位名。
import { useQuery } from '@tanstack/react-query'
import { jobs } from '../../api/endpoints'
import { JOBS_QUERY } from '../../queryKeys'
import type { LiveInput } from './runControl'

// 对话标题取第一条消息的开头，侧栏放得下。
const TITLE_LENGTH = 24

export function titleFrom(input: LiveInput): string {
  const firstLine = input.content.trim().split('\n')[0]?.trim()
  const name = firstLine || input.attachments[0]?.name || '新对话'
  return name.length > TITLE_LENGTH ? `${name.slice(0, TITLE_LENGTH)}…` : name
}

export function useJobTitle(): (jobId: string) => string {
  const list = useQuery({ queryKey: JOBS_QUERY, queryFn: ({ signal }) => jobs.list(signal) })
  return (jobId) => list.data?.find((job) => job.job_id === jobId)?.title ?? '未命名岗位'
}
