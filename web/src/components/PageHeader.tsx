// 页面头部：可选上方返回链接，下方左侧标题与说明、右侧操作区垂直居中对齐。
import { ArrowLeftOutlined } from '@ant-design/icons'
import { Button, Typography } from 'antd'
import type { ReactElement, ReactNode } from 'react'
import { Link } from 'react-router'

interface PageHeaderProps {
  title: string
  subtitle?: ReactNode
  backTo?: string
  backLabel?: string
  extra?: ReactNode
}

export function PageHeader({
  title,
  subtitle,
  backTo,
  backLabel,
  extra,
}: PageHeaderProps): ReactElement {
  return (
    <header className="page-header">
      {backTo && (
        <Link to={backTo}>
          <Button type="link" icon={<ArrowLeftOutlined />} className="back-link">
            {backLabel ?? '返回'}
          </Button>
        </Link>
      )}
      <div className="page-header-row">
        <div className="page-header-copy">
          <Typography.Title level={1}>{title}</Typography.Title>
          {subtitle && (
            <Typography.Text type="secondary" className="page-header-sub">
              {subtitle}
            </Typography.Text>
          )}
        </div>
        {extra && <div className="page-header-extra">{extra}</div>}
      </div>
    </header>
  )
}
