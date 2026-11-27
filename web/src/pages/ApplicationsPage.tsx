// 投递页：一次投递一张卡，提供看板与列表视图、搜索与筛选、直接改阶段及详情抽屉。
import { useQuery } from '@tanstack/react-query'
import { App, Button, Card, Checkbox, Input, Segmented, Skeleton, Typography } from 'antd'
import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import { useSearchParams } from 'react-router'
import { applications } from '../api/endpoints'
import { EmptyHint } from '../components/EmptyHint'
import { PageHeader } from '../components/PageHeader'
import { APPLICATIONS_QUERY } from '../queryKeys'
import { ApplicationDetailDrawer } from './application/ApplicationDetailDrawer'
import { ApplicationsBoard } from './application/ApplicationsBoard'
import { ApplicationsList } from './application/ApplicationsList'
import { RecordApplicationModal } from './application/RecordApplicationModal'
import {
  STALE_AFTER_DAYS,
  applicationAttemptNumbers,
  summarizeApplications,
  todayLocalDate,
  type ApplicationSortField,
} from './application/applicationStatus'
import { useRefreshApplications } from './application/useRefreshApplications'

type ViewMode = 'board' | 'list'

export function ApplicationsPage(): ReactElement {
  const { message } = App.useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const refresh = useRefreshApplications()
  const [search, setSearch] = useState('')
  const [sortField, setSortField] = useState<ApplicationSortField>('stage_changed_at')
  const [recording, setRecording] = useState(false)
  const missingNotifiedRef = useRef<string | null>(null)

  const view: ViewMode = searchParams.get('view') === 'list' ? 'list' : 'board'
  const showRejected = searchParams.get('rejected') === '1'
  const selectedId = searchParams.get('id')

  const list = useQuery({
    queryKey: APPLICATIONS_QUERY,
    queryFn: ({ signal }) => applications.list(undefined, signal),
  })

  const allItems = useMemo(() => list.data ?? [], [list.data])
  const today = todayLocalDate()
  const attemptNumbers = useMemo(() => applicationAttemptNumbers(allItems), [allItems])
  const visibleItems = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) return allItems
    return allItems.filter((item) => {
      const title = (item.job_snapshot_json.title ?? '').toLowerCase()
      const company = (item.job_snapshot_json.company ?? '').toLowerCase()
      return title.includes(keyword) || company.includes(keyword)
    })
  }, [allItems, search])

  const selectedRecord = useMemo(
    () =>
      selectedId ? (allItems.find((item) => item.application_id === selectedId) ?? null) : null,
    [allItems, selectedId],
  )

  useEffect(() => {
    if (!selectedId) {
      missingNotifiedRef.current = null
      return
    }
    if (!list.isSuccess || selectedRecord) return
    if (missingNotifiedRef.current !== selectedId) {
      missingNotifiedRef.current = selectedId
      void message.warning('这条投递不存在或已删除')
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('id')
        return next
      },
      { replace: true },
    )
  }, [list.isSuccess, message, selectedId, selectedRecord, setSearchParams])

  function updateParam(key: 'view' | 'rejected' | 'id', value: string | null): void {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value === null) next.delete(key)
      else next.set(key, value)
      return next
    })
  }

  const summary = summarizeApplications(visibleItems, today)
  const hiddenRejectedCount = visibleItems.filter((item) => item.stage === 'rejected').length

  return (
    <section className="page page-wide applications-page">
      <PageHeader
        title="投递"
        subtitle="每次投递一张卡，阶段跟着投递走。同一岗位投两次就有两张。"
        extra={
          <Button type="primary" onClick={() => setRecording(true)}>
            记录投递
          </Button>
        }
      />

      {list.isPending ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : list.isError ? (
        <Card>
          <EmptyHint
            description={list.error.message}
            action={<Button onClick={() => void list.refetch()}>重试</Button>}
          />
        </Card>
      ) : (
        <>
          <div className="applications-summary" aria-label="投递汇总">
            <div>
              <strong className="serif-heading tabular-num">{summary.total}</strong>
              <span>次投递</span>
            </div>
            <div>
              <strong className="serif-heading tabular-num">{summary.waiting}</strong>
              <span>条在等结果</span>
            </div>
            <div className="is-stale">
              <strong className="serif-heading tabular-num">{summary.stale}</strong>
              <span>条超过 {STALE_AFTER_DAYS} 天没有进展</span>
            </div>
            <div>
              <strong className="serif-heading tabular-num">{summary.offer}</strong>
              <span>个 Offer</span>
            </div>
          </div>

          <div className="applications-toolbar">
            <Segmented<ViewMode>
              aria-label="切换视图"
              value={view}
              options={[
                { label: '看板', value: 'board' },
                { label: '列表', value: 'list' },
              ]}
              onChange={(nextView) => updateParam('view', nextView === 'list' ? 'list' : null)}
            />
            <Input
              allowClear
              className="applications-search"
              placeholder="搜岗位名或公司"
              aria-label="搜岗位名或公司"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <Checkbox
              checked={showRejected}
              onChange={(event) => updateParam('rejected', event.target.checked ? '1' : null)}
            >
              显示未通过
            </Checkbox>
            <span className="applications-toolbar-hint">
              超过 {STALE_AFTER_DAYS} 天没有新进展的会标出来
            </span>
          </div>

          {allItems.length === 0 ? (
            <Card>
              <EmptyHint description="还没有投递记录。在岗位详情点「记录投递」，或在对话里说「我投了」。" />
            </Card>
          ) : visibleItems.length === 0 ? (
            <Card>
              <EmptyHint description="没有匹配的投递。" />
            </Card>
          ) : (
            <>
              {view === 'board' ? (
                <ApplicationsBoard
                  items={visibleItems}
                  showRejected={showRejected}
                  attemptNumbers={attemptNumbers}
                  today={today}
                  onSelect={(id) => updateParam('id', id)}
                  refresh={refresh}
                />
              ) : (
                <ApplicationsList
                  items={visibleItems}
                  showRejected={showRejected}
                  sortField={sortField}
                  onSortChange={setSortField}
                  attemptNumbers={attemptNumbers}
                  today={today}
                  onSelect={(id) => updateParam('id', id)}
                  refresh={refresh}
                />
              )}
              {!showRejected && hiddenRejectedCount > 0 && (
                <Typography.Paragraph type="secondary" className="app-rejected-hint">
                  另有 {hiddenRejectedCount} 条未通过，
                  <button
                    type="button"
                    className="app-link-btn"
                    onClick={() => updateParam('rejected', '1')}
                  >
                    显示
                  </button>
                </Typography.Paragraph>
              )}
            </>
          )}
        </>
      )}

      <ApplicationDetailDrawer
        record={selectedRecord}
        attemptNumber={
          selectedRecord ? attemptNumbers.get(selectedRecord.application_id) : undefined
        }
        open={Boolean(selectedRecord)}
        onClose={() => updateParam('id', null)}
        refresh={refresh}
      />

      <RecordApplicationModal
        open={recording}
        onClose={() => setRecording(false)}
        refresh={refresh}
      />
    </section>
  )
}
