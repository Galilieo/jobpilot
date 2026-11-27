// 投递页「记录投递」两步弹窗：先选一个有定制简历的岗位，再进入记录投递表单。
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Input, Modal, Skeleton, Typography } from 'antd'
import { useMemo, useState, type ReactElement } from 'react'
import { Link } from 'react-router'
import { tailoring } from '../../api/endpoints'
import type { TailoredResumeSummary } from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { MetaList } from '../../components/MetaList'
import { TAILORED_RESUMES_QUERY } from '../../queryKeys'
import { NewApplicationModal } from './ApplicationForm'

interface RecordApplicationModalProps {
  open: boolean
  onClose: () => void
  refresh: () => Promise<void>
}

interface TailoredJobOption {
  jobId: string
  title: string
  company: string | null
  currentVersion: number
  updatedAt: string
}

function buildJobOptions(items: TailoredResumeSummary[]): TailoredJobOption[] {
  const byJob = new Map<string, TailoredJobOption>()
  for (const item of items) {
    if (!item.job_id) continue
    const existing = byJob.get(item.job_id)
    if (existing && existing.updatedAt >= item.updated_at) continue
    byJob.set(item.job_id, {
      jobId: item.job_id,
      title: item.job_title ?? '未命名岗位',
      company: item.job_company,
      currentVersion: item.current_version,
      updatedAt: item.updated_at,
    })
  }
  return [...byJob.values()].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
}

export function RecordApplicationModal({
  open,
  onClose,
  refresh,
}: RecordApplicationModalProps): ReactElement | null {
  if (!open) return null
  return <RecordApplicationFlow onClose={onClose} refresh={refresh} />
}

function RecordApplicationFlow({
  onClose,
  refresh,
}: Omit<RecordApplicationModalProps, 'open'>): ReactElement {
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  const resumes = useQuery({
    queryKey: TAILORED_RESUMES_QUERY,
    queryFn: ({ signal }) => tailoring.listAll(signal),
  })

  const options = useMemo(() => buildJobOptions(resumes.data ?? []), [resumes.data])
  const filtered = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) return options
    return options.filter(
      (item) =>
        item.title.toLowerCase().includes(keyword) ||
        (item.company ?? '').toLowerCase().includes(keyword),
    )
  }, [options, search])

  if (selectedJobId) {
    return <NewApplicationModal jobId={selectedJobId} close={onClose} refresh={refresh} />
  }

  return (
    <Modal title="记录投递" open onCancel={onClose} footer={null} destroyOnHidden>
      {resumes.isPending ? (
        <Skeleton active paragraph={{ rows: 3 }} />
      ) : resumes.isError ? (
        <Alert
          type="error"
          title={resumes.error.message}
          action={<Button onClick={() => void resumes.refetch()}>重试</Button>}
        />
      ) : options.length === 0 ? (
        <EmptyHint
          description="先在对话里为一个岗位生成定制简历，才能记录投递"
          action={
            <Link to="/jobs" onClick={onClose}>
              去岗位库
            </Link>
          }
        />
      ) : (
        <div className="job-pick-step">
          <Typography.Text type="secondary">选择一个已有定制简历的岗位：</Typography.Text>
          <Input
            allowClear
            placeholder="搜岗位名或公司"
            aria-label="搜索可选岗位"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          {filtered.length === 0 ? (
            <Typography.Text type="secondary">没有匹配的岗位。</Typography.Text>
          ) : (
            <div className="job-pick-list">
              {filtered.map((item) => (
                <button
                  key={item.jobId}
                  type="button"
                  className="job-pick-item"
                  onClick={() => setSelectedJobId(item.jobId)}
                >
                  <span className="job-pick-title">{item.title}</span>
                  <MetaList
                    items={[
                      ...(item.company ? [item.company] : []),
                      `第 ${item.currentVersion} 版简历`,
                    ]}
                  />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
