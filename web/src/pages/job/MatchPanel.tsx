// 匹配报告：逐条要求的结论、依据和建议；支持查看与切换同岗位的历史匹配报告。
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Empty, Skeleton, Space, Statistic, Tag, Typography } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { jobs } from '../../api/endpoints'
import { newIdempotencyKey } from '../../api/http'
import type { Coverage, MatchReport, MatchSummary } from '../../api/types'
import { VERDICT, formatDateTime } from '../../labels'
import { OVERVIEW_QUERY, matchReportQuery, matchesQuery } from '../../queryKeys'

export function MatchPanel({ jobId }: { jobId: string }): ReactElement {
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

  const action = (
    <Button loading={match.isPending} onClick={() => match.mutate()}>
      {latestId ? '重新匹配' : '分析匹配度'}
    </Button>
  )

  return (
    <Card title="匹配报告" extra={action}>
      {summaries.isPending || (activeId && report.isPending) ? (
        <Skeleton active />
      ) : summaries.isError ? (
        <Empty description={summaries.error.message}>
          <Button onClick={() => void summaries.refetch()}>重试</Button>
        </Empty>
      ) : !activeId ? (
        <Empty description="用已发布的事实库逐条对照岗位要求，看哪些能写、哪些缺。">
          <Button loading={match.isPending} onClick={() => match.mutate()}>
            分析匹配度
          </Button>
        </Empty>
      ) : report.isError ? (
        <Empty description={report.error.message}>
          <Button onClick={() => void report.refetch()}>重试</Button>
        </Empty>
      ) : (
        report.data && (
          <Space orientation="vertical" size={16} className="full-width">
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
            <ReportBody report={report.data} />
            {summaries.data && summaries.data.length > 1 && (
              <MatchHistory
                items={summaries.data}
                activeId={activeId}
                latestId={latestId}
                onSelect={(reportId) =>
                  setSelectedReportId(reportId === latestId ? null : reportId)
                }
              />
            )}
          </Space>
        )
      )}
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
                <Space size={6} wrap>
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

function ReportBody({ report }: { report: MatchReport }): ReactElement {
  return (
    <>
      <Space size={32} wrap>
        <CoverageStat title="硬性要求" coverage={report.hard_summary} />
        <CoverageStat title="加分项" coverage={report.preferred_summary} />
      </Space>
      <Typography.Paragraph type="secondary" className="report-meta">
        基于第 {report.fact_set_version} 版事实库
      </Typography.Paragraph>
      {report.main_gaps.length > 0 && (
        <section>
          <Typography.Title level={5}>主要短板</Typography.Title>
          <ul className="report-gaps">
            {report.main_gaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        </section>
      )}
      <ul className="row-list">
        {report.items.map((item) => (
          <li key={item.requirement_id}>
            <Space size={8} wrap>
              <Tag color={VERDICT[item.verdict].color}>{VERDICT[item.verdict].text}</Tag>
              <Typography.Text strong>{item.requirement_text}</Typography.Text>
              {item.requirement_type === 'preferred' && <Tag>加分项</Tag>}
            </Space>
            <Typography.Paragraph className="report-reason">{item.reason}</Typography.Paragraph>
            {item.facts.length > 0 && (
              <Typography.Paragraph type="secondary" className="report-facts">
                依据：{item.facts.map((fact) => `${fact.text}（${fact.experience}）`).join('；')}
              </Typography.Paragraph>
            )}
            <Typography.Text type="secondary">建议：{item.suggestion}</Typography.Text>
          </li>
        ))}
      </ul>
    </>
  )
}
