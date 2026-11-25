// 「我的」：顶部三张概览卡，下方经历（0 条事实时内联导入，否则按钮弹窗导入）、简历列表与记忆三个标签页。
import { useQuery } from '@tanstack/react-query'
import { Card, Space, Tabs, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import { useSearchParams } from 'react-router'
import { facts, tailoring } from '../api/endpoints'
import { EmptyHint } from '../components/EmptyHint'
import { PageHeader } from '../components/PageHeader'
import { LIBRARY_QUERY, TAILORED_RESUMES_QUERY } from '../queryKeys'
import { FactLibrary } from './profile/FactLibrary'
import { ImportResumeModal, ResumeImport } from './profile/ResumeImport'
import { TailoredResumeList } from './profile/TailoredResumeList'

type ProfileTab = 'facts' | 'resumes' | 'memories'

function parseTab(value: string | null): ProfileTab {
  if (value === 'resumes' || value === 'memories') return value
  return 'facts'
}

export function ProfilePage(): ReactElement {
  const [searchParams, setSearchParams] = useSearchParams()
  const [importOpen, setImportOpen] = useState(false)
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
    (library.data?.draft.items.filter((item) => item.status === 'pending').length ?? 0) +
    (library.data?.draft.criteria.filter((item) => item.status === 'pending').length ?? 0) +
    (library.data?.draft.education.filter((item) => item.draft_status === 'pending').length ?? 0)
  const resumeCount = resumes.data?.length ?? 0
  const hasAnyFact = Boolean(
    library.data &&
    (library.data.facts.length > 0 ||
      library.data.criteria.length > 0 ||
      library.data.education.length > 0 ||
      library.data.draft.items.length > 0 ||
      library.data.draft.criteria.length > 0 ||
      library.data.draft.education.length > 0),
  )

  function switchTab(next: string): void {
    setSearchParams(next === 'facts' ? {} : { tab: next }, { replace: true })
  }

  return (
    <section className="page">
      <PageHeader title="我的" subtitle="改写只引用你确认并发布过的经历事实，不会编造。" />

      <div className="profile-overview">
        <Card size="small" className="profile-stat-card">
          <Typography.Text type="secondary">经历事实</Typography.Text>
          <div className="workspace-metric">
            <span>{library.isPending ? '—' : publishedCount}</span>
            <Typography.Text type="secondary" className="workspace-metric-sub">
              条 · {version ? `v${version}` : '未发布'}
              {pendingCount > 0 ? ` · ${pendingCount} 条待确认` : ''}
            </Typography.Text>
          </div>
        </Card>
        <Card size="small" className="profile-stat-card">
          <Typography.Text type="secondary">定制简历</Typography.Text>
          <div className="workspace-metric">
            <span>{resumes.isPending ? '—' : resumeCount}</span>
            <Typography.Text type="secondary" className="workspace-metric-sub">
              份
            </Typography.Text>
          </div>
        </Card>
        <Card size="small" className="profile-stat-card">
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
                {!library.isPending && !library.isError && !hasAnyFact && <ResumeImport />}
                <FactLibrary onOpenImport={hasAnyFact ? () => setImportOpen(true) : undefined} />
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
                <EmptyHint description="偏好记忆将在后续版本开放。记忆只保存求职与写作偏好，不保存经历。" />
              </Card>
            ),
          },
        ]}
      />
      <ImportResumeModal open={importOpen} onClose={() => setImportOpen(false)} />
    </section>
  )
}
