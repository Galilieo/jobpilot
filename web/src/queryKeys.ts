// React Query 的查询键，集中放一处：改数据的地方用同一个键让查询失效。
export const LIBRARY_QUERY = ['library'] as const
export const JOBS_QUERY = ['jobs'] as const

export const jobQuery = (jobId: string) => ['jobs', jobId] as const
export const matchesQuery = (jobId: string) => ['jobs', jobId, 'matches'] as const
export const tailorRunsQuery = (jobId: string) => ['jobs', jobId, 'tailor-runs'] as const
