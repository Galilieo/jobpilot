// 岗位库：岗位卡片列表；「添加岗位」粘贴 JD，解析完进入岗位详情。
import { PlusOutlined, WarningOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Empty, Input, Modal, Skeleton, Tag, Typography } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { useNavigate } from 'react-router'
import { jobs } from '../api/endpoints'
import { newIdempotencyKey } from '../api/http'
import type { JobStage } from '../api/types'
import { JOBS_QUERY } from '../queryKeys'

const STAGE: Record<JobStage, string> = {
  saved: '已收藏',
  applied: '已投递',
  interviewing: '面试中',
  offer: '已拿 offer',
  rejected: '未通过',
  archived: '已归档',
}

export function JobsPage(): ReactElement {
  const navigate = useNavigate()
  const [adding, setAdding] = useState(false)
  const list = useQuery({ queryKey: JOBS_QUERY, queryFn: ({ signal }) => jobs.list(signal) })

  return (
    <section className="page">
      <header className="page-heading page-heading-actions">
        <div>
          <Typography.Title level={1}>岗位库</Typography.Title>
          <Typography.Text type="secondary">
            贴入 JD 整理岗位要求，再用确认过的经历做匹配、生成定制简历。
          </Typography.Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setAdding(true)}>
          添加岗位
        </Button>
      </header>
      {list.isPending ? (
        <Skeleton active />
      ) : list.isError ? (
        <Empty description={list.error.message} />
      ) : list.data.length === 0 ? (
        <Card>
          <Empty description="还没有岗位。贴入一份 JD 开始。">
            <Button onClick={() => setAdding(true)}>添加岗位</Button>
          </Empty>
        </Card>
      ) : (
        <div className="job-grid">
          {list.data.map((job) => (
            <Card
              key={job.job_id}
              hoverable
              className="job-card"
              onClick={() => void navigate(`/jobs/${job.job_id}`)}
            >
              <Typography.Text strong>{job.title ?? '未命名岗位'}</Typography.Text>
              <Typography.Paragraph type="secondary" className="job-company">
                {job.company ?? '公司未填写'}
              </Typography.Paragraph>
              <div className="job-tags">
                <Tag color="blue">{STAGE[job.stage]}</Tag>
                {job.injection_count > 0 && (
                  <Tag color="warning" icon={<WarningOutlined />}>
                    JD 里有可疑指令
                  </Tag>
                )}
                {job.keywords.slice(0, 4).map((keyword) => (
                  <Tag key={keyword}>{keyword}</Tag>
                ))}
              </div>
            </Card>
          ))}
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
    onError: (error) => message.error(error.message), // 保留已输入的内容
  })

  return (
    <Modal
      title="添加岗位"
      open={open}
      onCancel={onClose}
      okText="解析并添加"
      cancelText="取消"
      okButtonProps={{ disabled: !text.trim() }}
      confirmLoading={create.isPending}
      onOk={() => create.mutate()}
      destroyOnHidden={false}
    >
      <Typography.Paragraph type="secondary">
        把招聘页上的岗位名称、职责和任职要求整段复制进来，解析通常需要十几秒。
      </Typography.Paragraph>
      <Input.TextArea
        value={text}
        onChange={(event) => setText(event.target.value)}
        autoSize={{ minRows: 8, maxRows: 16 }}
        placeholder="JD 原文"
      />
    </Modal>
  )
}
