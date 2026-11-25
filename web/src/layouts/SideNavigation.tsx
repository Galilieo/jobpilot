// 导航入口和账号菜单；还未接入会话服务时，最近对话只显示一行浅灰提示文字。
import {
  AppstoreOutlined,
  DownOutlined,
  LogoutOutlined,
  PlusOutlined,
  SettingOutlined,
  SolutionOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Avatar, Button, Dropdown, Menu, Typography, theme } from 'antd'
import type { MenuProps } from 'antd'
import type { ReactElement } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router'
import { auth } from '../api/endpoints'
import { ME_QUERY, useCurrentUser } from '../auth'

interface SideNavigationProps {
  onNavigate?: () => void
}

const navigationItems = [
  { key: '/workspace', icon: <AppstoreOutlined />, label: '工作台' },
  { key: '/jobs', icon: <SolutionOutlined />, label: '岗位库' },
  { key: '/me', icon: <UserOutlined />, label: '我的' },
] satisfies MenuProps['items']

export function SideNavigation({ onNavigate }: SideNavigationProps): ReactElement {
  const { token } = theme.useToken()
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const email = useCurrentUser().data?.email

  async function logout(): Promise<void> {
    await auth.logout().catch(() => undefined) // 已过期也照样回到登录页
    queryClient.clear() // 不把上一个账号的数据留在缓存里
    await queryClient.invalidateQueries({ queryKey: ME_QUERY })
    void navigate('/login', { replace: true })
  }
  const selectedKey = navigationItems.find(({ key }) => pathname.startsWith(key))?.key

  function openPage(destination: string): void {
    void navigate(destination)
    onNavigate?.()
  }

  return (
    <nav className="sidebar" aria-label="主导航">
      <div className="brand">
        Job<span style={{ color: token.colorPrimary }}>Pilot</span>
      </div>
      <Button icon={<PlusOutlined />} block onClick={() => openPage('/conversations/new')}>
        新对话
      </Button>
      <Menu
        className="sidebar-menu"
        mode="inline"
        items={navigationItems}
        selectedKeys={selectedKey ? [selectedKey] : []}
        onClick={({ key }) => openPage(key)}
      />
      <section className="recent-conversations" aria-label="最近对话">
        <Typography.Text type="secondary">最近对话</Typography.Text>
        <Typography.Text type="secondary" className="recent-empty">
          还没有对话
        </Typography.Text>
      </section>
      <Dropdown
        trigger={['click']}
        placement="topLeft"
        menu={{
          items: [
            { key: '/account', icon: <SettingOutlined />, label: '账号设置' },
            { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
          ],
          onClick: ({ key }) => (key === 'logout' ? void logout() : openPage(key)),
        }}
      >
        <Button type="text" className="account-menu" aria-label="打开账号菜单">
          <Avatar icon={<UserOutlined />} style={{ background: token.colorPrimary }} />
          <span className="account-copy">
            <Typography.Text>我的账号</Typography.Text>
            <Typography.Text type="secondary" ellipsis>
              {email}
            </Typography.Text>
          </span>
          <DownOutlined />
        </Button>
      </Dropdown>
    </nav>
  )
}
