// 列表行：支持悬停或键盘聚焦显示行内操作（窄屏/触屏常驻），以及「⋯」更多菜单和左侧警示色条。
import { EllipsisOutlined } from '@ant-design/icons'
import { Button, Dropdown, type MenuProps } from 'antd'
import { useState, type ReactElement, type ReactNode } from 'react'

interface HoverRowProps {
  children: ReactNode
  primaryAction?: ReactNode
  actions?: ReactNode
  menuItems?: MenuProps['items']
  menuLabel?: string
  highlight?: 'warning' | 'none'
  className?: string
}

export function HoverRow({
  children,
  primaryAction,
  actions,
  menuItems,
  menuLabel = '更多操作',
  highlight = 'none',
  className,
}: HoverRowProps): ReactElement {
  const [menuOpen, setMenuOpen] = useState(false)
  const hasMenu = Boolean(menuItems && menuItems.length > 0)
  const hasHoverActions = Boolean(actions || hasMenu)
  const rowClass = ['hover-row', highlight === 'warning' ? 'row-pending' : '', className ?? '']
    .filter(Boolean)
    .join(' ')

  return (
    <li tabIndex={0} className={rowClass}>
      <div className="hover-row-body">{children}</div>
      {(primaryAction || hasHoverActions) && (
        <div className="hover-row-controls">
          {primaryAction && <div className="hover-row-primary">{primaryAction}</div>}
          {hasHoverActions && (
            <div
              className={
                menuOpen ? 'hover-row-actions hover-row-actions-open' : 'hover-row-actions'
              }
            >
              {actions}
              {hasMenu && (
                <Dropdown
                  trigger={['click']}
                  open={menuOpen}
                  onOpenChange={setMenuOpen}
                  menu={{ items: menuItems }}
                >
                  <Button
                    size="small"
                    type="text"
                    icon={<EllipsisOutlined />}
                    aria-label={menuLabel}
                  />
                </Dropdown>
              )}
            </div>
          )}
        </div>
      )}
    </li>
  )
}
