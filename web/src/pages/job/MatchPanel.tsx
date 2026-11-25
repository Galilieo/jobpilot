// 匹配报告：顶部展示硬性与加分项已覆盖数及主要短板，下方逐条列出依据和建议；支持历史切换。
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Typography,
} from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { jobs } from '../../api/endpoints'
import { newIdempotencyKey } from '../../api/http'
import type { Coverage, MatchReport, MatchSummary } from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { VERDICT, formatDateTime } from '../../labels'
import { OVERVIEW_QUERY, matchReportQuery, matchesQuery } from '../../queryKeys'

export function MatchPanel({
  jobId,
  embedded = false,
}: {
  jobId: string
  embedded?: boolean
}): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const key = useRef(newIdempotencyKey())
  const [selectedReportId, setSelectedReportId] = useState<string | null>(null)

  const summaries = useQuery({
    queryKey: matchesQuery(jobId),
    queryFn: ({ signal }) => jobs.matches(jobId, signal),
  })
  const latestId = summaries.data?.[0]?.report_id
  const activeId = selectedReportId ?? latestId
  const viewingOld = Boolean(selectedReportId && latestId && selectedReportId !== latestId)

  const report = useQuery({
    queryKey: matchReportQuery(activeId ?? ''),
    queryFn: ({ signal }) => jobs.report(activeId!, signal),
    enabled: Boolean(activeId),
  })
  const match = useMutation({
    mutationFn: () => jobs.match(jobId, key.current),
    onSuccess: async () => {
      key.current = newIdempotencyKey()
      setSelectedReportId(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: matchesQuery(jobId) }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
      ])
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  const rematchButton = (
    <Button loading={match.isPending} onClick={() => match.mutate()}>
      {latestId ? '重新匹配' : '分析匹配度'}
    </Button>
  )

  const content =
    summaries.isPending || (activeId && report.isPending) ? (
      <Skeleton active />
    ) : summaries.isError ? (
      <EmptyHint
        description={summaries.error.message}
        action={<Button onClick={() => void summaries.refetch()}>重试</Button>}
      />
    ) : !activeId ? (
      <EmptyHint
        description="用已发布的事实库逐条对照岗位要求，看哪些能写、哪些缺。"
        action={
          <Button loading={match.isPending} onClick={() => match.mutate()}>
            分析匹配度
          </Button>
        }
      />
    ) : report.isError ? (
      <EmptyHint
        description={report.error.message}
        action={<Button onClick={() => void report.refetch()}>重试</Button>}
      />
    ) : (
      report.data && (
        <Space orientation="vertical" size={16} className="full-width">
          {embedded && (
            <div className="match-bar">
              <Typography.Text type="secondary" className="report-meta">
                基于第 {report.data.fact_set_version} 版事实库
              </Typography.Text>
              {rematchButton}
            </div>
          )}
          {viewingOld && (
            <Alert
              type="info"
              showIcon
              title={`正在查看历史报告（基于第 ${report.data.fact_set_version} 版事实库）`}
              action={
                <Button size="small" onClick={() => setSelectedReportId(null)}>
                  回到最新报告
                </Button>
              }
            />
          )}
          <ReportBody report={report.data} showVersion={!embedded} />
          {summaries.data && summaries.data.length > 1 && (
            <MatchHistory
              items={summaries.data}
              activeId={activeId}
              latestId={latestId}
              onSelect={(reportId) => setSelectedReportId(reportId === latestId ? null : reportId)}
            />
          )}
        </Space>
      )
    )

  if (embedded) return <>{content}</>

  return (
    <Card title="匹配报告" extra={rematchButton}>
      {content}
    </Card>
  )
}

function MatchHistory({
  items,
  activeId,
  latestId,
  onSelect,
}: {
  items: MatchSummary[]
  activeId: string
  latestId: string | undefined
  onSelect: (reportId: string) => void
}): ReactElement {
  return (
    <section className="match-history">
      <Typography.Title level={5}>匹配历史</Typography.Title>
      <ul className="row-list">
        {items.map((item) => {
          const isCurrent = item.report_id === activeId
          const hardDone = item.hard_summary.covered + item.hard_summary.expression_gap
          const prefDone = item.preferred_summary.covered + item.preferred_summary.expression_gap
          return (
            <li key={item.report_id} className="fact-item">
              <div className="fact-row">
                <Space size={8} wrap>
                  <Typography.Text strong={isCurrent} className="tabular-num">
                    {formatDateTime(item.created_at)}
                  </Typography.Text>
                  <Tag>v{item.fact_set_version}</Tag>
                  {item.report_id === latestId && <Tag color="processing">最新</Tag>}
                </Space>
                <Typography.Text type="secondary" className="tabular-num">
                  硬性 {hardDone}/{item.hard_summary.total} · 加分 {prefDone}/
                  {item.preferred_summary.total}
                </Typography.Text>
              </div>
              <Button size="small" disabled={isCurrent} onClick={() => onSelect(item.report_id)}>
                {isCurrent ? '当前查看' : '查看报告'}
              </Button>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function CoverageStat({ title, coverage }: { title: string; coverage: Coverage }): ReactElement {
  return (
    <Statistic
      title={title}
      value={coverage.covered + coverage.expression_gap}
      suffix={`/ ${coverage.total} 条已覆盖`}
      className="coverage-stat"
    />
  )
}

function ReportBody({
  report,
  showVersion,
}: {
  report: MatchReport
  showVersion: boolean
}): ReactElement {
  const topGaps = report.main_gaps.slice(0, 3)
  const extraGaps = report.main_gaps.slice(3)

  return (
    <>
      <Space size={32} wrap>
        <CoverageStat title="硬性要求" coverage={report.hard_summary} />
        <CoverageStat title="加分项" coverage={report.preferred_summary} />
      </Space>
      {showVersion && (
        <Typography.Paragraph type="secondary" className="report-meta">
          基于第 {report.fact_set_version} 版事实库
        </Typography.Paragraph>
      )}
      {topGaps.length > 0 && (
        <section>
          <Typography.Title level={5}>主要短板</Typography.Title>
          <ul className="report-gaps">
            {topGaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
          {extraGaps.length > 0 && (
            <Collapse
              ghost
              size="small"
              items={[
                {
                  key: 'more-gaps',
                  label: `展开其余 ${extraGaps.length} 条短板`,
                  children: (
                    <ul className="report-gaps">
                      {extraGaps.map((gap) => (
                        <li key={gap}>{gap}</li>
                      ))}
                    </ul>
                  ),
                },
              ]}
            />
          )}
        </section>
      )}
      <ul className="row-list">
        {report.items.map((item) => {
          const evidence = [
            ...(item.criteria ?? []).map((c) => `${c.label} · ${c.text}`),
            ...item.facts.map((fact) => `${fact.text}（${fact.experience}）`),
          ]
          return (
            <li key={item.requirement_id}>
              <Space size={8} wrap>
                <Tag color={VERDICT[item.verdict].color}>{VERDICT[item.verdict].text}</Tag>
                <Typography.Text strong>{item.requirement_text}</Typography.Text>
                {item.requirement_type === 'preferred' && <Tag>加分项</Tag>}
                {item.decision_source === 'program' && <Tag color="cyan">按数字比对</Tag>}
                {item.review_note && <Tag color="warning">待核对</Tag>}
              </Space>
              <Typography.Paragraph className="report-reason">{item.reason}</Typography.Paragraph>
              {item.review_note && (
                <Typography.Paragraph type="warning" className="report-reason">
                  待核对说明：{item.review_note}
                </Typography.Paragraph>
              )}
              {evidence.length > 0 && (
                <Typography.Paragraph type="secondary" className="report-facts">
                  依据：{evidence.join('；')}
                </Typography.Paragraph>
              )}
              <Typography.Text type="secondary">建议：{item.suggestion}</Typography.Text>
            </li>
          )
        })}
      </ul>
    </>
  )
}
