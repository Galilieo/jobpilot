// 导航入口、最近对话和账号菜单。
import {
  AppstoreOutlined,
  LogoutOutlined,
  PlusOutlined,
  SettingOutlined,
  SolutionOutlined,
  UpOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Avatar, Button, Dropdown, Menu, Typography, theme } from 'antd'
import type { MenuProps } from 'antd'
import { useEffect, useRef, useState, type ReactElement } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router'
import { auth } from '../api/endpoints'
import { ME_QUERY, useCurrentUser } from '../auth'
import { RecentConversations } from './RecentConversations'

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
  const [menuOpen, setMenuOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        setMenuOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [menuOpen])

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
      <div className="brand serif-heading">
        Job<span style={{ color: token.colorPrimary }}>Pilot</span>
      </div>
      <Button
        className="new-chat-btn"
        icon={<PlusOutlined />}
        block
        onClick={() => openPage('/conversations/new')}
      >
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
        <RecentConversations onNavigate={onNavigate} />
      </section>
      <Dropdown
        trigger={['click']}
        placement="topLeft"
        open={menuOpen}
        onOpenChange={setMenuOpen}
        menu={{
          items: [
            { key: '/account', icon: <SettingOutlined />, label: '账号设置' },
            { type: 'divider' },
            { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
          ],
          onClick: ({ key }) => {
            setMenuOpen(false)
            if (key === 'logout') void logout()
            else openPage(key)
          },
        }}
      >
        <Button
          ref={triggerRef}
          type="text"
          className="account-menu"
          aria-label="打开账号菜单"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
        >
          <Avatar icon={<UserOutlined />} style={{ background: token.colorPrimary }} />
          <span className="account-copy">
            <Typography.Text>我的账号</Typography.Text>
            <Typography.Text type="secondary" ellipsis>
              {email}
            </Typography.Text>
          </span>
          <UpOutlined className="account-chevron" />
        </Button>
      </Dropdown>
    </nav>
  )
}
