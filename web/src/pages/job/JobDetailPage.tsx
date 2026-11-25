// 岗位详情：左边岗位信息和匹配报告，右边定制简历（产物面板，07 第 4 节）。
// 对话 Agent 接上后，左边换成对话，右边的面板不变。
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Button, Empty, Skeleton, Typography } from 'antd'
import type { ReactElement } from 'react'
import { Link, useParams } from 'react-router'
import { jobs } from '../../api/endpoints'
import { jobQuery } from '../../queryKeys'
import { JobInfo } from './JobInfo'
import { MatchPanel } from './MatchPanel'
import { ResumePanel } from './ResumePanel'

export function JobDetailPage(): ReactElement {
  const { jobId = '' } = useParams()
  const job = useQuery({
    queryKey: jobQuery(jobId),
    queryFn: ({ signal }) => jobs.get(jobId, signal),
  })

  return (
    <section className="page page-wide">
      <Link to="/jobs">
        <Button type="link" icon={<ArrowLeftOutlined />} className="back-link">
          岗位库
        </Button>
      </Link>
      {job.isPending ? (
        <Skeleton active />
      ) : job.isError ? (
        <Empty description={job.error.message} />
      ) : (
        <>
          <header className="page-heading">
            <Typography.Title level={1}>
              {job.data.parsed.basic_fields.title ?? '未命名岗位'}
            </Typography.Title>
            <Typography.Text type="secondary">
              {job.data.parsed.basic_fields.company ?? '公司未填写'}
            </Typography.Text>
          </header>
          <div className="job-detail">
            <div className="job-detail-main">
              <JobInfo job={job.data} />
              <MatchPanel jobId={jobId} />
            </div>
            <div className="job-detail-panel">
              <ResumePanel jobId={jobId} />
            </div>
          </div>
        </>
      )}
    </section>
  )
}
