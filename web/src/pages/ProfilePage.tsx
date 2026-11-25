// 「我的」：经历（导入简历、确认事实、发布版本）；简历和记忆两栏之后接入。
import { Empty, Space, Tabs, Typography } from 'antd'
import type { ReactElement } from 'react'
import { FactLibrary } from './profile/FactLibrary'
import { ResumeImport } from './profile/ResumeImport'

export function ProfilePage(): ReactElement {
  return (
    <section className="page">
      <header className="page-heading">
        <Typography.Title level={1}>我的</Typography.Title>
        <Typography.Text type="secondary">
          改写只引用你确认并发布过的经历事实，不会编造。
        </Typography.Text>
      </header>
      <Tabs
        items={[
          {
            key: 'facts',
            label: '经历',
            children: (
              <Space orientation="vertical" size={16} className="full-width">
                <ResumeImport />
                <FactLibrary />
              </Space>
            ),
          },
          {
            key: 'resumes',
            label: '简历',
            children: <Empty description="定制简历在对应岗位的详情页里查看和编辑。" />,
          },
          {
            key: 'memories',
            label: '记忆',
            children: <Empty description="偏好记忆准备中，记忆只保存偏好，不保存经历。" />,
          },
        ]}
      />
    </section>
  )
}
