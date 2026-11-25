// 集中存放尚未接业务接口的页面；只建立导航与内容槽位，不展示虚构业务结果。
import { Card, Empty, Typography } from 'antd'
import type { ReactElement, ReactNode } from 'react'
import { Link } from 'react-router'

interface PlaceholderProps {
  title: string
  description: ReactNode
}

function PagePlaceholder({ title, description }: PlaceholderProps): ReactElement {
  return (
    <section className="page">
      <header className="page-heading">
        <Typography.Title level={1}>{title}</Typography.Title>
      </header>
      <Card>
        <Empty className="page-empty" description={description} />
      </Card>
    </section>
  )
}

export { AccountPage } from './AccountPage'

export function NotFoundPage(): ReactElement {
  return (
    <PagePlaceholder
      title="找不到这个页面"
      description={
        <>
          可以<Link to="/workspace">返回工作台</Link>继续。
        </>
      }
    />
  )
}
