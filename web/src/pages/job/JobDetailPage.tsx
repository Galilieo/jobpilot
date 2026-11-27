// 岗位详情：头部展示核心信息与进度；左栏分「匹配报告」「岗位信息」「JD 原文」标签页，右栏为定制简历面板。
import { EllipsisOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App,
  Button,
  Card,
  Drawer,
  Dropdown,
  Grid,
  Skeleton,
  Space,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { useState, type ReactElement } from 'react'
import { useNavigate, useParams } from 'react-router'
import { jobs } from '../../api/endpoints'
import { EmptyHint } from '../../components/EmptyHint'
import { MetaList } from '../../components/MetaList'
import { PageHeader } from '../../components/PageHeader'
import { JOB_STAGE, jobMetaParts } from '../../labels'
import {
  APPLICATIONS_QUERY,
  JOBS_QUERY,
  OVERVIEW_QUERY,
  TAILORED_RESUMES_QUERY,
  jobQuery,
  matchesQuery,
} from '../../queryKeys'
import { ApplicationRecords } from './ApplicationRecords'
import { InjectionAlert, JobInfo } from './JobInfo'
import { MatchPanel } from './MatchPanel'
import { ResumePanel } from './ResumePanel'

type DetailTab = 'match' | 'info' | 'raw'

export function JobDetailPage(): ReactElement {
  const { message, modal } = App.useApp()
  const { jobId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const screens = Grid.useBreakpoint()
  const isNarrow = screens.lg === false
  const [resumeOpen, setResumeOpen] = useState(false)
  const [manualTab, setManualTab] = useState<DetailTab | null>(null)

  const job = useQuery({
    queryKey: jobQuery(jobId),
    queryFn: ({ signal }) => jobs.get(jobId, signal),
  })
  const matches = useQuery({
    queryKey: matchesQuery(jobId),
    queryFn: ({ signal }) => jobs.matches(jobId, signal),
  })

  const updateArchive = useMutation({
    mutationFn: (archived: boolean) => jobs.update(jobId, { archived }),
    onSuccess: async (_, archived) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: jobQuery(jobId) }),
        queryClient.invalidateQueries({ queryKey: JOBS_QUERY }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
      ])
      message.success(archived ? '已归档岗位' : '已取消归档')
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  const removeJob = useMutation({
    mutationFn: () => jobs.remove(jobId),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: jobQuery(jobId) })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: JOBS_QUERY }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
        queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
        queryClient.invalidateQueries({ queryKey: APPLICATIONS_QUERY }),
      ])
      message.success('已删除岗位')
      void navigate('/jobs', { replace: true })
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  function confirmDelete(): void {
    modal.confirm({
      title: '删除这个岗位？',
      content: '会一起删除这个岗位的匹配报告、改写记录和定制简历。已记录的投递及简历快照会保留。',
      okText: '删除岗位',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => removeJob.mutateAsync(),
    })
  }

  const hasReport = Boolean(matches.data && matches.data.length > 0)
  const activeTab: DetailTab = manualTab ?? (hasReport ? 'match' : 'info')
  const fields = job.data?.parsed.basic_fields
  const subtitle = fields ? (
    <MetaList
      items={jobMetaParts({
        company: fields.company,
        location: fields.location,
        salary: fields.salary,
      })}
    />
  ) : undefined

  return (
    <section className="page page-wide">
      {job.isPending ? (
        <>
          <PageHeader title="岗位详情" backTo="/jobs" backLabel="岗位库" />
          <Skeleton active />
        </>
      ) : job.isError ? (
        <>
          <PageHeader title="岗位详情" backTo="/jobs" backLabel="岗位库" />
          <Card>
            <EmptyHint
              description={job.error.message}
              action={<Button onClick={() => void job.refetch()}>重试</Button>}
            />
          </Card>
        </>
      ) : (
        <>
          <PageHeader
            backTo="/jobs"
            backLabel="岗位库"
            title={fields?.title ?? '未命名岗位'}
            subtitle={subtitle}
            extra={
              <Space size={8} wrap>
                <Tag>{JOB_STAGE[job.data.stage]}</Tag>
                <Dropdown
                  trigger={['click']}
                  menu={{
                    items: [
                      {
                        key: 'archive',
                        label: job.data.stage === 'archived' ? '取消归档' : '归档岗位',
                        disabled: updateArchive.isPending,
                        onClick: () => updateArchive.mutate(job.data.stage !== 'archived'),
                      },
                      { type: 'divider' },
                      {
                        key: 'delete',
                        label: '删除岗位',
                        onClick: confirmDelete,
                      },
                    ],
                  }}
                >
                  <Button icon={<EllipsisOutlined />} aria-label="更多岗位操作" />
                </Dropdown>
                {isNarrow && <Button onClick={() => setResumeOpen(true)}>查看定制简历</Button>}
              </Space>
            }
          />
          <div className="job-detail">
            <div className="job-detail-main">
              <Card>
                <Tabs
                  activeKey={activeTab}
                  onChange={(key) => setManualTab(key as DetailTab)}
                  items={[
                    {
                      key: 'match',
                      label: '匹配报告',
                      children: <MatchPanel jobId={jobId} embedded />,
                    },
                    {
                      key: 'info',
                      label: '岗位信息',
                      children: <JobInfo job={job.data} />,
                    },
                    {
                      key: 'raw',
                      label: 'JD 原文',
                      children: (
                        <Space orientation="vertical" size={16} className="full-width">
                          <InjectionAlert flags={job.data.injection_flags} />
                          <Typography.Paragraph className="job-raw">
                            {job.data.raw_text}
                          </Typography.Paragraph>
                        </Space>
                      ),
                    },
                  ]}
                />
              </Card>
              <ApplicationRecords key={jobId} jobId={jobId} />
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
              destroyOnHidden
            >
              <ResumePanel jobId={jobId} />
            </Drawer>
          )}
        </>
      )}
    </section>
  )
}
