// 定制简历面板：生成（后台运行，轮询进度）→ 可编辑的预览 → 导出。
// 预览页由后端渲染，内嵌编辑器自动保存；这里只负责运行的状态和入口。
import { ExpandOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Dropdown,
  Empty,
  Form,
  Modal,
  Select,
  Skeleton,
  Steps,
  Typography,
} from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { tailoring } from '../../api/endpoints'
import { ApiError, newIdempotencyKey } from '../../api/http'
import type { PlanBlock, SectionKind, TailorRun } from '../../api/types'
import { RUN_STATUS, SECTION, STOP_REASON } from '../../labels'
import { tailorRunsQuery } from '../../queryKeys'

const POLL_MS = 2000

export function ResumePanel({ jobId }: { jobId: string }): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const key = useRef(newIdempotencyKey())
  const [unsectioned, setUnsectioned] = useState<string[] | null>(null)

  const runs = useQuery({
    queryKey: tailorRunsQuery(jobId),
    queryFn: ({ signal }) => tailoring.runs(jobId, signal),
  })
  const latest = runs.data?.[0]
  const run = useQuery({
    queryKey: ['tailor-run', latest?.run_id],
    queryFn: ({ signal }) => tailoring.run(latest!.run_id, signal),
    enabled: Boolean(latest),
    // 运行在后台执行：没结束就每两秒看一次，结束后停。
    refetchInterval: (query) => (query.state.data?.status === 'running' ? POLL_MS : false),
  })
  const finished = run.data && run.data.status !== 'running'
  if (finished && latest?.status === 'running') {
    void queryClient.invalidateQueries({ queryKey: tailorRunsQuery(jobId) })
  }

  const start = useMutation({
    mutationFn: (sections: Record<string, SectionKind>) =>
      tailoring.start(jobId, sections, key.current),
    onSuccess: async () => {
      key.current = newIdempotencyKey() // 下次「重新生成」是新的一次
      setUnsectioned(null)
      await queryClient.invalidateQueries({ queryKey: tailorRunsQuery(jobId) })
    },
    onError: (error) => {
      const detail = error instanceof ApiError ? error.detail : null
      if (detail && typeof detail === 'object' && 'experiences' in detail) {
        setUnsectioned(detail.experiences as string[]) // 让用户指定栏目后重试
        return
      }
      message.error(error.message)
    },
  })

  const generate = (
    <Button
      type={latest ? 'default' : 'primary'}
      icon={latest ? <ReloadOutlined /> : undefined}
      loading={start.isPending}
      disabled={run.data?.status === 'running'}
      onClick={() => start.mutate({})}
    >
      {latest ? '重新生成' : '生成定制简历'}
    </Button>
  )

  return (
    <Card title="定制简历" extra={generate} className="resume-panel">
      {runs.isPending ? (
        <Skeleton active />
      ) : !latest ? (
        <Empty description="按这个岗位改写你的经历：每一条都引用已确认的事实，检查不过的会退回原文。" />
      ) : !run.data ? (
        <Skeleton active />
      ) : (
        <RunView run={run.data} hasResume={latest.has_resume || run.data.status === 'succeeded'} />
      )}
      <SectionsModal
        experiences={unsectioned}
        submitting={start.isPending}
        onCancel={() => setUnsectioned(null)}
        onSubmit={(sections) => {
          key.current = newIdempotencyKey() // 请求内容变了，用新键
          start.mutate(sections)
        }}
      />
    </Card>
  )
}

function RunView({ run, hasResume }: { run: TailorRun; hasResume: boolean }): ReactElement {
  if (run.status === 'running') return <Progress blocks={run.blocks} />
  if (run.status === 'succeeded' && hasResume) return <Preview runId={run.run_id} />
  return (
    <>
      <Alert
        type={run.status === 'budget_exhausted' ? 'warning' : 'error'}
        showIcon
        message={RUN_STATUS[run.status]}
        description={
          (run.stop_reason && STOP_REASON[run.stop_reason]) ??
          '这次没有生成完整简历，可以重新生成。'
        }
      />
      {run.bullets.length > 0 && (
        <section className="partial-bullets">
          <Typography.Title level={5}>已写好的条目</Typography.Title>
          <ul>
            {run.bullets.map((bullet) => (
              <li key={bullet.bullet_id}>{bullet.text}</li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}

function Progress({ blocks }: { blocks: PlanBlock[] }): ReactElement {
  return (
    <div className="tailor-progress">
      <Steps
        orientation="vertical"
        size="small"
        current={1}
        items={[
          { title: '对照岗位要求', content: '读取事实库和岗位，分析匹配' },
          { title: '逐段改写并检查', content: '每条都核对引用的事实，不过就修订或退回原文' },
          { title: '组装完整简历' },
        ]}
      />
      {blocks.length === 0 && (
        <Typography.Text type="secondary">
          通常需要半分钟到一分钟，可以先离开，回来再看。
        </Typography.Text>
      )}
    </div>
  )
}

function Preview({ runId }: { runId: string }): ReactElement {
  const url = tailoring.previewUrl(runId)
  return (
    <div className="resume-preview">
      <div className="resume-preview-actions">
        <Typography.Text type="secondary">点「编辑」直接修改，自动保存。</Typography.Text>
        <Button icon={<ExpandOutlined />} href={url} target="_blank">
          全屏编辑
        </Button>
        <Dropdown
          menu={{
            items: [
              {
                key: 'html',
                label: <a href={tailoring.exportUrl(runId, 'html')}>HTML（可继续编辑）</a>,
              },
              { key: 'docx', label: <a href={tailoring.exportUrl(runId, 'docx')}>Word（DOCX）</a> },
              { key: 'md', label: <a href={tailoring.exportUrl(runId, 'md')}>Markdown</a> },
            ],
          }}
        >
          <Button type="primary">导出</Button>
        </Dropdown>
      </div>
      <iframe title="定制简历预览" src={url} className="resume-frame" />
    </div>
  )
}

interface SectionsModalProps {
  experiences: string[] | null
  submitting: boolean
  onCancel: () => void
  onSubmit: (sections: Record<string, SectionKind>) => void
}

function SectionsModal({
  experiences,
  submitting,
  onCancel,
  onSubmit,
}: SectionsModalProps): ReactElement {
  const [form] = Form.useForm<Record<string, SectionKind>>()
  return (
    <Modal
      title="这些经历属于哪一栏？"
      open={experiences !== null}
      okText="继续生成"
      cancelText="取消"
      confirmLoading={submitting}
      onCancel={onCancel}
      onOk={() => void form.validateFields().then(onSubmit)}
      destroyOnHidden
    >
      <Typography.Paragraph type="secondary">
        这几段经历的事实是之前补充的，没有记录栏目。选好后会按栏目改写和排版。
      </Typography.Paragraph>
      <Form form={form} layout="vertical">
        {experiences?.map((experience) => (
          <Form.Item
            key={experience}
            name={experience}
            label={experience}
            rules={[{ required: true, message: '请选择' }]}
          >
            <Select
              options={(Object.keys(SECTION) as SectionKind[]).map((value) => ({
                value,
                label: SECTION[value],
              }))}
            />
          </Form.Item>
        ))}
      </Form>
    </Modal>
  )
}
