// 我的 · 简历：每份定制简历一行，展示对应岗位、事实库版本、当前版本号与状态。
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Skeleton, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { ReactElement } from 'react'
import { useNavigate } from 'react-router'
import { tailoring } from '../../api/endpoints'
import type { TailoredResumeSummary } from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { TAILORED_RESUME_STATUS, formatDateTime } from '../../labels'
import { TAILORED_RESUMES_QUERY } from '../../queryKeys'

export function TailoredResumeList(): ReactElement {
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: TAILORED_RESUMES_QUERY,
    queryFn: ({ signal }) => tailoring.listAll(signal),
  })

  if (query.isPending) return <Skeleton active />
  if (query.isError) {
    return (
      <Card>
        <EmptyHint
          description={query.error.message}
          action={<Button onClick={() => void query.refetch()}>重试</Button>}
        />
      </Card>
    )
  }
  if (query.data.length === 0) {
    return (
      <Card>
        <EmptyHint
          description="还没有定制简历。打开一个岗位并生成定制简历后，会显示在这里。"
          action={
            <Button type="primary" onClick={() => void navigate('/jobs')}>
              打开岗位库
            </Button>
          }
        />
      </Card>
    )
  }

  const columns: ColumnsType<TailoredResumeSummary> = [
    {
      title: '岗位',
      key: 'job',
      render: (_, record) => (
        <div>
          <Typography.Text strong>{record.job_title ?? '未命名岗位'}</Typography.Text>
          <div>
            <Typography.Text type="secondary">{record.job_company ?? '公司未填写'}</Typography.Text>
          </div>
        </div>
      ),
    },
    {
      title: '事实库版本',
      key: 'fact_set_version',
      render: (_, record) => {
        const stale =
          record.latest_fact_set_version !== null &&
          record.fact_set_version !== null &&
          record.fact_set_version < record.latest_fact_set_version
        return (
          <Space size={6} wrap>
            <span className="tabular-num">
              {record.fact_set_version !== null ? `v${record.fact_set_version}` : '—'}
            </span>
            {stale && <Tag color="warning">事实库已更新</Tag>}
          </Space>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (status: TailoredResumeSummary['status']) => (
        <Tag color={TAILORED_RESUME_STATUS[status].color}>
          {TAILORED_RESUME_STATUS[status].text}
        </Tag>
      ),
    },
    {
      title: '当前版本',
      dataIndex: 'current_version',
      key: 'current_version',
      render: (version: number) => <span className="tabular-num">v{version}</span>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      render: (value: string) => (
        <Typography.Text type="secondary" className="tabular-num">
          {formatDateTime(value)}
        </Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      render: (_, record) => (
        <Space size={8}>
          {record.job_id && (
            <Button size="small" onClick={() => void navigate(`/jobs/${record.job_id}`)}>
              打开
            </Button>
          )}
          <Button size="small" href={tailoring.exportUrl(record.run_id, 'html')}>
            导出 HTML
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <Card className="table-card">
      <Table
        rowKey="resume_id"
        columns={columns}
        dataSource={query.data}
        pagination={{ hideOnSinglePage: true }}
        scroll={{ x: 'max-content' }}
      />
    </Card>
  )
}
