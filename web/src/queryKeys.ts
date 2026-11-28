// React Query 的查询键，集中放一处：改数据的地方用同一个键让查询失效。
export const OVERVIEW_QUERY = ['overview'] as const
export const LIBRARY_QUERY = ['library'] as const
export const JOBS_QUERY = ['jobs'] as const
export const jobsQuery = (q?: string) => (q ? (['jobs', { q }] as const) : JOBS_QUERY)
export const TAILORED_RESUMES_QUERY = ['tailored-resumes'] as const

export const jobQuery = (jobId: string) => ['jobs', jobId] as const
export const matchesQuery = (jobId: string) => ['jobs', jobId, 'matches'] as const
export const matchReportQuery = (reportId: string) => ['match-report', reportId] as const
export const tailorRunsQuery = (jobId: string) => ['jobs', jobId, 'tailor-runs'] as const
export const tailorRunQuery = (runId: string) => ['tailor-run', runId] as const
export const tailorResumeQuery = (runId: string, version?: number) =>
  ['tailor-resume', runId, version ?? 'current'] as const
export const CONVERSATIONS_QUERY = ['conversations', 'list'] as const
export const conversationQuery = (conversationId: string) =>
  ['conversations', conversationId] as const
// 带上消息数和更新时间：一轮结束或压缩写完摘要后自动重新估算。
export const contextUsageQuery = (conversationId: string, messages: number, updatedAt: string) =>
  [...conversationQuery(conversationId), 'context', messages, updatedAt] as const
export const MEMORIES_QUERY = ['memories'] as const
export const WRITING_PREFERENCE_QUERY = ['writing-preference'] as const

export const APPLICATIONS_QUERY = ['applications'] as const
export const applicationsQuery = (jobId: string) => [...APPLICATIONS_QUERY, 'job', jobId] as const
export const applicationQuery = (id: string) => [...APPLICATIONS_QUERY, 'record', id] as const
export const applicationDiffQuery = (id: string) => [...applicationQuery(id), 'diff'] as const
