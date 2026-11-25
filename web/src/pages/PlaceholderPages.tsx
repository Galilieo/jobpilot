// 集中存放尚未接业务接口的页面；只建立导航与内容槽位，不展示虚构业务结果。
import { Card } from 'antd'
import type { ReactElement, ReactNode } from 'react'
import { Link } from 'react-router'
import { EmptyHint } from '../components/EmptyHint'
import { PageHeader } from '../components/PageHeader'

interface PlaceholderProps {
  title: string
  description: ReactNode
}

function PagePlaceholder({ title, description }: PlaceholderProps): ReactElement {
  return (
    <section className="page">
      <PageHeader title={title} />
      <Card>
        <EmptyHint description={description} />
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
