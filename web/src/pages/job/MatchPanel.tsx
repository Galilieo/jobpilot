// 匹配报告：逐条要求的结论、依据和建议；硬性要求和加分项分开计数，不给笼统的匹配度分数。
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Empty, List, Skeleton, Space, Statistic, Tag, Typography } from 'antd'
import { useRef, type ReactElement } from 'react'
import { jobs } from '../../api/endpoints'
import { newIdempotencyKey } from '../../api/http'
import type { Coverage, MatchReport } from '../../api/types'
import { VERDICT } from '../../labels'
import { matchesQuery } from '../../queryKeys'

export function MatchPanel({ jobId }: { jobId: string }): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const key = useRef(newIdempotencyKey())
  const summaries = useQuery({
    queryKey: matchesQuery(jobId),
    queryFn: ({ signal }) => jobs.matches(jobId, signal),
  })
  const latestId = summaries.data?.[0]?.report_id
  const report = useQuery({
    queryKey: ['match-report', latestId],
    queryFn: ({ signal }) => jobs.report(latestId!, signal),
    enabled: Boolean(latestId),
  })
  const match = useMutation({
    mutationFn: () => jobs.match(jobId, key.current),
    onSuccess: async () => {
      key.current = newIdempotencyKey()
      await queryClient.invalidateQueries({ queryKey: matchesQuery(jobId) })
    },
    onError: (error) => message.error(error.message),
  })

  const action = (
    <Button loading={match.isPending} onClick={() => match.mutate()}>
      {latestId ? '重新匹配' : '分析匹配度'}
    </Button>
  )

  return (
    <Card title="匹配报告" extra={action}>
      {summaries.isPending || (latestId && report.isPending) ? (
        <Skeleton active />
      ) : !latestId ? (
        <Empty description="用已发布的事实库逐条对照岗位要求，看哪些能写、哪些缺。" />
      ) : report.isError ? (
        <Empty description={report.error.message} />
      ) : (
        report.data && <ReportBody report={report.data} />
      )}
    </Card>
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
      <List
        itemLayout="vertical"
        dataSource={report.items}
        renderItem={(item) => (
          <List.Item key={item.requirement_id}>
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
          </List.Item>
        )}
      />
    </>
  )
}
