// 岗位投递记录、阶段历史以及只读快照的入口。
import { useQuery } from '@tanstack/react-query'
import { Alert, App, Button, Card, Drawer, Skeleton, Space, Tag, Timeline, Typography } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { applications } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import type { ApplicationRecord } from '../../api/types'
import { MetaList } from '../../components/MetaList'
import { APPLICATION_STAGE } from '../../labels'
import { APPLICATIONS_QUERY, applicationQuery, applicationsQuery } from '../../queryKeys'
import { ApplicationDiffDrawer } from './ApplicationDiffDrawer'
import { EditApplicationModal, NewApplicationModal } from './ApplicationForm'
import { useRefreshApplications } from './useRefreshApplications'

export function ApplicationRecords({ jobId }: { jobId: string }): ReactElement {
  const refresh = useRefreshApplications()
  const [adding, setAdding] = useState(false)
  const query = useQuery({
    queryKey: applicationsQuery(jobId),
    queryFn: ({ signal }) => applications.list(jobId, signal),
  })

  return (
    <Card
      title="投递记录"
      className="application-records"
      extra={
        <Button type="primary" onClick={() => setAdding(true)}>
          记录投递
        </Button>
      }
    >
      {query.isPending && <Skeleton active paragraph={{ rows: 2 }} />}
      {query.isError && (
        <Alert
          type="error"
          title={query.error.message}
          action={<Button onClick={() => void query.refetch()}>重试</Button>}
        />
      )}
      {query.data?.length === 0 && (
        <Typography.Text type="secondary">这个岗位还没有投递记录。</Typography.Text>
      )}
      {query.data?.map((record) => (
        <ApplicationRow key={record.application_id} record={record} refresh={refresh} />
      ))}
      {adding && (
        <NewApplicationModal jobId={jobId} close={() => setAdding(false)} refresh={refresh} />
      )}
    </Card>
  )
}

export function DetachedApplicationsDrawer({ close }: { close: () => void }): ReactElement {
  const refresh = useRefreshApplications()
  const query = useQuery({
    queryKey: APPLICATIONS_QUERY,
    queryFn: ({ signal }) => applications.list(undefined, signal),
    select: (records) => records.filter((record) => record.user_job_id === null),
  })
  return (
    <Drawer title="已删除岗位的投递" size={560} open onClose={close}>
      {query.isPending && <Skeleton active />}
      {query.isError && <Alert type="error" title={query.error.message} />}
      {query.data?.length === 0 && (
        <Typography.Text type="secondary">没有来自已删除岗位的投递。</Typography.Text>
      )}
      {query.data?.map((record) => (
        <ApplicationRow key={record.application_id} record={record} refresh={refresh} />
      ))}
    </Drawer>
  )
}

export function ApplicationRow({
  record,
  refresh,
  showJobInfo,
}: {
  record: ApplicationRecord
  refresh: () => Promise<void>
  showJobInfo?: boolean
}): ReactElement {
  const { modal, message } = App.useApp()
  const [historyOpen, setHistoryOpen] = useState(false)
  const [editing, setEditing] = useState<ApplicationRecord | null>(null)
  const [panel, setPanel] = useState<'snapshot' | 'diff' | null>(null)

  function confirmDelete(): void {
    modal.confirm({
      title: '删除这条投递记录？',
      content: '这次投递的阶段记录和简历快照也会删除。',
      okText: '删除投递',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          await applications.remove(record.application_id, record.revision)
          await refresh()
        } catch (error) {
          if (error instanceof ApiError && error.status === 409) {
            await refresh()
            void message.warning('投递记录刚被别处改过，已重新读取')
            return
          }
          void message.error(error instanceof Error ? error.message : '删除失败')
        }
      },
    })
  }

  const version = record.resume_version != null ? [`第 ${record.resume_version} 版简历`] : []
  const shouldShowJobInfo = showJobInfo ?? record.user_job_id === null

  return (
    <article className="application-row">
      {shouldShowJobInfo && (
        <>
          <Typography.Text strong>{record.job_snapshot_json.title ?? '未命名岗位'}</Typography.Text>
          <MetaList
            items={[
              record.job_snapshot_json.company,
              record.job_snapshot_json.location,
              record.job_snapshot_json.salary,
            ].filter((field): field is string => Boolean(field))}
          />
        </>
      )}
      <Space wrap size={8}>
        <Typography.Text strong className="tabular-num">
          {record.applied_at}
        </Typography.Text>
        <Tag>{APPLICATION_STAGE[record.stage]}</Tag>
        <MetaList items={[...(record.channel ? [record.channel] : []), ...version]} />
      </Space>
      {record.backfilled && (
        <Typography.Text type="secondary">迁移前记录的进度，没有当时的简历</Typography.Text>
      )}
      <Space wrap size={[6, 4]}>
        <Button size="small" onClick={() => setEditing(record)}>
          编辑投递
        </Button>
        {!record.backfilled && (
          <>
            <Button size="small" onClick={() => setPanel('snapshot')}>
              查看当时的简历
            </Button>
            <Button size="small" onClick={() => setPanel('diff')}>
              和现在对比
            </Button>
          </>
        )}
        <Button size="small" type="text" danger onClick={confirmDelete}>
          删除投递
        </Button>
      </Space>
      <details
        className="application-history"
        onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
      >
        <summary>阶段记录</summary>
        {historyOpen && <StageHistory applicationId={record.application_id} />}
      </details>
      {editing && (
        <EditApplicationModal record={editing} close={() => setEditing(null)} refresh={refresh} />
      )}
      {panel === 'snapshot' && <ApplicationSnapshot record={record} close={() => setPanel(null)} />}
      {panel === 'diff' && (
        <ApplicationDiffDrawer id={record.application_id} close={() => setPanel(null)} />
      )}
    </article>
  )
}

function StageHistory({ applicationId }: { applicationId: string }): ReactElement {
  const detail = useQuery({
    queryKey: applicationQuery(applicationId),
    queryFn: ({ signal }) => applications.detail(applicationId, signal),
  })
  if (detail.isPending) return <Skeleton active paragraph={false} />
  if (detail.isError) return <Alert type="error" title={detail.error.message} />
  return (
    <Timeline
      items={detail.data.stage_events.map((event) => ({
        key: event.event_id,
        color: 'green',
        content: `${event.occurred_at} · ${event.from_stage ? `${APPLICATION_STAGE[event.from_stage]} → ` : ''}${APPLICATION_STAGE[event.to_stage]}`,
      }))}
    />
  )
}

function ApplicationSnapshot({
  record,
  close,
}: {
  record: ApplicationRecord
  close: () => void
}): ReactElement {
  const frame = useRef<HTMLIFrameElement>(null)
  const [loaded, setLoaded] = useState(false)
  return (
    <Drawer
      title={`投递简历 · 第 ${record.resume_version} 版`}
      size={850}
      open
      onClose={close}
      extra={
        <Button disabled={!loaded} onClick={() => frame.current?.contentWindow?.print()}>
          打印 / 保存 PDF
        </Button>
      }
    >
      <iframe
        ref={frame}
        className="application-snapshot"
        title="投递简历快照"
        src={applications.snapshotUrl(record.application_id)}
        sandbox="allow-same-origin allow-modals"
        onLoad={() => setLoaded(true)}
      />
    </Drawer>
  )
}
