// 投递记录变更后统一刷新投递列表、岗位库与工作台缓存。
import { useQueryClient } from '@tanstack/react-query'
import { APPLICATIONS_QUERY, JOBS_QUERY, OVERVIEW_QUERY } from '../../queryKeys'

export function useRefreshApplications(): () => Promise<void> {
  const queryClient = useQueryClient()
  return async () => {
    await Promise.all(
      [APPLICATIONS_QUERY, JOBS_QUERY, OVERVIEW_QUERY].map((queryKey) =>
        queryClient.invalidateQueries({ queryKey }),
      ),
    )
  }
}
