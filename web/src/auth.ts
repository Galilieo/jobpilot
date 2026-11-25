// 当前登录用户。整个应用共用一个查询；登录、退出后让它失效重新读取。
import { useQuery } from '@tanstack/react-query'
import { auth } from './api/endpoints'
import { ApiError } from './api/http'

export const ME_QUERY = ['me'] as const

export function useCurrentUser() {
  return useQuery({
    queryKey: ME_QUERY,
    queryFn: ({ signal }) => auth.me(signal),
    retry: (count, error) => !(error instanceof ApiError && error.status === 401) && count < 2,
    staleTime: Infinity,
    refetchOnWindowFocus: 'always',
  })
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}
