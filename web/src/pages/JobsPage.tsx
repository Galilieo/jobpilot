// 岗位库：岗位卡片列表；「添加岗位」支持粘贴、上传文件或截图，解析完进入岗位详情。
import { PlusOutlined, WarningOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Modal, Skeleton, Tag, Typography, theme } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { useNavigate } from 'react-router'
import { jobs } from '../api/endpoints'
import { newIdempotencyKey } from '../api/http'
import { EmptyHint } from '../components/EmptyHint'
import { MetaList } from '../components/MetaList'
import { PageHeader } from '../components/PageHeader'
import { TextSourceInput } from '../components/TextSourceInput'
import { JOB_STAGE, formatMatchSummary, jobMetaParts } from '../labels'
import { JOBS_QUERY } from '../queryKeys'

const JD_COPY = {
  filePrompt: '点击或拖入 JD 文件',
  screenshotsPrompt: '点击或拖入 JD 截图，可以多张',
  pasteHint: '把招聘页上的岗位名称、职责和任职要求整段复制进来，解析通常需要十几秒。',
  placeholder: 'JD 原文',
}

export function JobsPage(): ReactElement {
  const { token } = theme.useToken()
  const navigate = useNavigate()
  const [adding, setAdding] = useState(false)
  const list = useQuery({ queryKey: JOBS_QUERY, queryFn: ({ signal }) => jobs.list(signal) })
  const hasJobs = Boolean(list.data && list.data.length > 0)

  return (
    <section className="page">
      <PageHeader
        title="岗位库"
        subtitle="贴入 JD 整理岗位要求，再用确认过的经历做匹配、生成定制简历。"
        extra={
          hasJobs && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setAdding(true)}>
              添加岗位
            </Button>
          )
        }
      />
      {list.isPending ? (
        <Skeleton active />
      ) : list.isError ? (
        <Card>
          <EmptyHint
            description={list.error.message}
            action={<Button onClick={() => void list.refetch()}>重试</Button>}
          />
        </Card>
      ) : list.data.length === 0 ? (
        <Card>
          <EmptyHint
            description="还没有岗位。贴入一份 JD 开始。"
            action={
              <Button type="primary" onClick={() => setAdding(true)}>
                添加岗位
              </Button>
            }
          />
        </Card>
      ) : (
        <div className="job-grid">
          {list.data.map((job) => {
            const extraKeywords = Math.max(0, job.keywords.length - 3)
            return (
              <Card
                key={job.job_id}
                hoverable
                className="job-card"
                onClick={() => void navigate(`/jobs/${job.job_id}`)}
              >
                <div className="job-card-head">
                  <Typography.Text strong className="job-card-title">
                    {job.title ?? '未命名岗位'}
                  </Typography.Text>
                  {job.salary ? (
                    <Typography.Text
                      strong
                      className="job-card-salary"
                      style={{ color: token.colorPrimary }}
                    >
                      {job.salary}
                    </Typography.Text>
                  ) : (
                    <Typography.Text type="secondary" className="job-card-salary">
                      薪资未填写
                    </Typography.Text>
                  )}
                </div>
                <MetaList
                  className="job-company"
                  items={jobMetaParts({
                    company: job.company,
                    location: job.location,
                    onsite_days_per_week: job.onsite_days_per_week,
                    education: job.education,
                  })}
                />
                {(job.keywords.length > 0 || job.injection_count > 0) && (
                  <div className="job-tags">
                    {job.keywords.slice(0, 3).map((keyword) => (
                      <Tag key={keyword}>{keyword}</Tag>
                    ))}
                    {extraKeywords > 0 && <Tag>+{extraKeywords}</Tag>}
                    {job.injection_count > 0 && (
                      <Tag color="warning" icon={<WarningOutlined />}>
                        JD 里有可疑指令
                      </Tag>
                    )}
                  </div>
                )}
                <div className="job-card-foot">
                  <Typography.Text type="secondary" className="tabular-num">
                    {formatMatchSummary(job.match_summary)}
                  </Typography.Text>
                  <Typography.Text type="secondary">{JOB_STAGE[job.stage]}</Typography.Text>
                </div>
              </Card>
            )
          })}
        </div>
      )}
      <AddJobModal open={adding} onClose={() => setAdding(false)} />
    </section>
  )
}

function AddJobModal({ open, onClose }: { open: boolean; onClose: () => void }): ReactElement {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const [reading, setReading] = useState(false)
  const [segmentedKey, setSegmentedKey] = useState(0)
  // 同一次添加的重试用同一个键；同样的正文再贴一次，服务端直接返回已有的岗位。
  const key = useRef(newIdempotencyKey())

  const create = useMutation({
    mutationFn: () => jobs.create(text, key.current),
    onSuccess: async (job) => {
      key.current = newIdempotencyKey()
      setText('')
      onClose()
      await queryClient.invalidateQueries({ queryKey: JOBS_QUERY })
      void navigate(`/jobs/${job.job_id}`)
    },
    onError: (error) => {
      void message.error(error.message) // 保留已输入的内容
    },
  })

  return (
    <Modal
      title="添加岗位"
      open={open}
      onCancel={onClose}
      afterOpenChange={(visible) => {
        if (visible) setSegmentedKey((prev) => prev + 1)
      }}
      width={720}
      okText="解析岗位"
      cancelText="取消"
      okButtonProps={{ disabled: !text.trim() || reading }}
      confirmLoading={create.isPending}
      onOk={() => create.mutate()}
      styles={{ body: { maxHeight: '65vh', overflowY: 'auto' } }}
      destroyOnHidden
    >
      <TextSourceInput
        value={text}
        onChange={setText}
        onReadingChange={setReading}
        copy={JD_COPY}
        maxRows={10}
        segmentedKey={segmentedKey}
      />
    </Modal>
  )
}
