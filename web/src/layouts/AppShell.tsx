// 共享侧栏与主区；窄屏仅把同一份导航放进抽屉，不另建一套路由。
import { MenuOutlined } from '@ant-design/icons'
import { Button, Drawer, Grid, Layout, Result, Spin, theme } from 'antd'
import { useState, type ReactElement } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router'
import { isUnauthorized, useCurrentUser } from '../auth'
import { PageErrorBoundary } from '../components/PageErrorBoundary'
import { AccountRecoveryPage } from '../pages/AccountPage'
import { SideNavigation } from './SideNavigation'

export function AppShell(): ReactElement {
  const { token } = theme.useToken()
  const screens = Grid.useBreakpoint()
  // 与 index.css 里的 @media (max-width: 991px) 对应，改断点时两处一起改。
  const isNarrow = screens.lg === false
  const [navigationOpen, setNavigationOpen] = useState(false)
  const user = useCurrentUser()
  const location = useLocation()

  // 没登录就去登录页，登录后回到原来要去的页面。
  if (user.isPending) return <Spin fullscreen />
  if (isUnauthorized(user.error)) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  if (user.isError) {
    return (
      <Result
        status="warning"
        title="暂时无法连接服务"
        subTitle="请确认后端已启动，稍后刷新页面重试。"
        extra={<Button onClick={() => void user.refetch()}>重新连接</Button>}
      />
    )
  }

  if (user.data?.deletion_deadline) {
    return <AccountRecoveryPage deadline={user.data.deletion_deadline} />
  }

  return (
    <Layout className="app-shell" style={{ color: token.colorText, fontFamily: token.fontFamily }}>
      {!isNarrow && (
        <Layout.Sider width={232}>
          <SideNavigation />
        </Layout.Sider>
      )}
      <Layout className="app-main">
        {isNarrow && (
          <header
            className="mobile-header"
            style={{ borderBottom: `1px solid ${token.colorBorderSecondary}` }}
          >
            <Button
              type="text"
              icon={<MenuOutlined />}
              aria-label="打开导航"
              onClick={() => setNavigationOpen(true)}
            />
            <span className="brand serif-heading">
              Job<span style={{ color: token.colorPrimary }}>Pilot</span>
            </span>
          </header>
        )}
        <Layout.Content className="app-content">
          {/* 按路径重置：一页出错不影响侧栏，换到别的页面后错误提示自动消失。 */}
          <PageErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </PageErrorBoundary>
        </Layout.Content>
      </Layout>
      <Drawer
        title="导航"
        placement="left"
        size={260}
        open={isNarrow && navigationOpen}
        onClose={() => setNavigationOpen(false)}
        styles={{ body: { padding: 0, background: token.colorBgLayout } }}
      >
        <SideNavigation onNavigate={() => setNavigationOpen(false)} />
      </Drawer>
    </Layout>
  )
}
