// 组件测试共用的渲染封装：提供主题、Ant Design App 上下文、关闭重试的 QueryClient 与路由。
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderResult } from '@testing-library/react'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import type { ReactElement } from 'react'
import { MemoryRouter, type InitialEntry } from 'react-router'
import { jobpilotTheme } from '../theme'

export function renderWithProviders(
  ui: ReactElement,
  { initialEntries = ['/'] }: { initialEntries?: InitialEntry[] } = {},
): RenderResult & { queryClient: QueryClient } {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  const result = render(
    <ConfigProvider locale={zhCN} theme={jobpilotTheme}>
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>,
  )
  return { ...result, queryClient }
}
