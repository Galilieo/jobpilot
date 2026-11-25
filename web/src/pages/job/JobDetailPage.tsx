// 岗位详情：左边岗位信息和匹配报告，右边定制简历（窄屏时通过顶部「查看结果」打开全屏层）。
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Drawer, Empty, Grid, Select, Skeleton, Space, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import { Link, useParams } from 'react-router'
import { jobs } from '../../api/endpoints'
import type { JobStage } from '../../api/types'
import { JOB_STAGE } from '../../labels'
import { JOBS_QUERY, OVERVIEW_QUERY, jobQuery } from '../../queryKeys'
import { JobInfo } from './JobInfo'
import { MatchPanel } from './MatchPanel'
import { ResumePanel } from './ResumePanel'

const STAGE_OPTIONS = (Object.keys(JOB_STAGE) as JobStage[]).map((value) => ({
  value,
  label: JOB_STAGE[value],
}))

export function JobDetailPage(): ReactElement {
  const { message } = App.useApp()
  const { jobId = '' } = useParams()
  const queryClient = useQueryClient()
  const screens = Grid.useBreakpoint()
  const isNarrow = screens.lg === false
  const [resumeOpen, setResumeOpen] = useState(false)

  const job = useQuery({
    queryKey: jobQuery(jobId),
    queryFn: ({ signal }) => jobs.get(jobId, signal),
  })

  const updateStage = useMutation({
    mutationFn: (stage: JobStage) => jobs.update(jobId, { stage }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: jobQuery(jobId) }),
        queryClient.invalidateQueries({ queryKey: JOBS_QUERY }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
      ])
      message.success('已更新岗位进度')
    },
    onError: (error) => {
      void message.error(error.message)
    },
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
        <Empty description={job.error.message}>
          <Button onClick={() => void job.refetch()}>重试</Button>
        </Empty>
      ) : (
        <>
          <header className="page-heading page-heading-actions">
            <div>
              <Typography.Title level={1}>
                {job.data.parsed.basic_fields.title ?? '未命名岗位'}
              </Typography.Title>
              <Typography.Text type="secondary">
                {job.data.parsed.basic_fields.company ?? '公司未填写'}
              </Typography.Text>
            </div>
            <Space wrap>
              <Select<JobStage>
                aria-label="岗位进度"
                value={job.data.stage}
                loading={updateStage.isPending}
                options={STAGE_OPTIONS}
                onChange={(stage) => updateStage.mutate(stage)}
              />
              {isNarrow && <Button onClick={() => setResumeOpen(true)}>查看结果</Button>}
            </Space>
          </header>
          <div className="job-detail">
            <div className="job-detail-main">
              <JobInfo job={job.data} />
              <MatchPanel jobId={jobId} />
            </div>
            {!isNarrow && (
              <div className="job-detail-panel">
                <ResumePanel jobId={jobId} />
              </div>
            )}
          </div>
          {isNarrow && (
            <Drawer
              title="定制简历"
              placement="right"
              size="100%"
              open={resumeOpen}
              onClose={() => setResumeOpen(false)}
              styles={{ body: { padding: 16 } }}
            >
              <ResumePanel jobId={jobId} />
            </Drawer>
          )}
        </>
      )}
    </section>
  )
}
