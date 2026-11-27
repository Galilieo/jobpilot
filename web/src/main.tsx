// 只在入口创建一次服务上下文；切换页面时保留查询缓存与主题。
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import 'antd/dist/reset.css'
import './index.css'
import App from './App.tsx'
import { PageErrorBoundary } from './components/PageErrorBoundary.tsx'
import { jobpilotTheme } from './theme.ts'

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* 不在两个汉字的按钮中间插空格（默认会显示成「打 开」） */}
    <ConfigProvider locale={zhCN} theme={jobpilotTheme} button={{ autoInsertSpace: false }}>
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            {/* 兜底：侧栏或登录页本身出错时，也不留下一整页空白。 */}
            <PageErrorBoundary>
              <App />
            </PageErrorBoundary>
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
)
