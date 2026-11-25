// 「我的」：顶部三张概览卡，下方经历（导入、补充、确认发布）、简历列表与记忆三个标签页。
import { useQuery } from '@tanstack/react-query'
import { Card, Empty, Space, Tabs, Typography } from 'antd'
import type { ReactElement } from 'react'
import { useSearchParams } from 'react-router'
import { facts, tailoring } from '../api/endpoints'
import { LIBRARY_QUERY, TAILORED_RESUMES_QUERY } from '../queryKeys'
import { FactLibrary } from './profile/FactLibrary'
import { ResumeImport } from './profile/ResumeImport'
import { TailoredResumeList } from './profile/TailoredResumeList'

type ProfileTab = 'facts' | 'resumes' | 'memories'

function parseTab(value: string | null): ProfileTab {
  if (value === 'resumes' || value === 'memories') return value
  return 'facts'
}

export function ProfilePage(): ReactElement {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab = parseTab(searchParams.get('tab'))
  const library = useQuery({
    queryKey: LIBRARY_QUERY,
    queryFn: ({ signal }) => facts.library(signal),
  })
  const resumes = useQuery({
    queryKey: TAILORED_RESUMES_QUERY,
    queryFn: ({ signal }) => tailoring.listAll(signal),
  })

  const publishedCount = library.data?.facts.length ?? 0
  const version = library.data?.fact_set?.version
  const pendingCount =
    library.data?.draft.items.filter((item) => item.status === 'pending').length ?? 0
  const resumeCount = resumes.data?.length ?? 0

  function switchTab(next: string): void {
    setSearchParams(next === 'facts' ? {} : { tab: next }, { replace: true })
  }

  return (
    <section className="page">
      <header className="page-heading">
        <Typography.Title level={1}>我的</Typography.Title>
        <Typography.Text type="secondary">
          改写只引用你确认并发布过的经历事实，不会编造。
        </Typography.Text>
      </header>

      <div className="profile-overview">
        <Card
          size="small"
          hoverable
          className={`profile-stat-card ${activeTab === 'facts' ? 'profile-stat-card-active' : ''}`}
          onClick={() => switchTab('facts')}
        >
          <Typography.Text type="secondary">经历事实</Typography.Text>
          <div className="workspace-metric">
            <span>{library.isPending ? '—' : publishedCount}</span>
            <Typography.Text type="secondary" className="workspace-metric-sub">
              条 · {version ? `v${version}` : '未发布'}
              {pendingCount > 0 ? ` · ${pendingCount} 条待确认` : ''}
            </Typography.Text>
          </div>
        </Card>
        <Card
          size="small"
          hoverable
          className={`profile-stat-card ${activeTab === 'resumes' ? 'profile-stat-card-active' : ''}`}
          onClick={() => switchTab('resumes')}
        >
          <Typography.Text type="secondary">定制简历</Typography.Text>
          <div className="workspace-metric">
            <span>{resumes.isPending ? '—' : resumeCount}</span>
            <Typography.Text type="secondary" className="workspace-metric-sub">
              份
            </Typography.Text>
          </div>
        </Card>
        <Card
          size="small"
          hoverable
          className={`profile-stat-card ${activeTab === 'memories' ? 'profile-stat-card-active' : ''}`}
          onClick={() => switchTab('memories')}
        >
          <Typography.Text type="secondary">记忆</Typography.Text>
          <div className="workspace-metric">
            <Typography.Text type="secondary">之后开放</Typography.Text>
          </div>
        </Card>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={switchTab}
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
            children: <TailoredResumeList />,
          },
          {
            key: 'memories',
            label: '记忆',
            children: (
              <Card>
                <Empty description="偏好记忆将在后续版本开放。记忆只保存求职与写作偏好，不保存经历。" />
              </Card>
            ),
          },
        ]}
      />
    </section>
  )
}
