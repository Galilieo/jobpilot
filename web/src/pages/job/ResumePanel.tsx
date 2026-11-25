// 定制简历面板：生成进度（后台轮询）、可编辑预览、依据追溯、建议补入、版本与运行历史。
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
  Space,
  Steps,
  Tabs,
  Typography,
  theme,
} from 'antd'
import { useEffect, useRef, useState, type ReactElement } from 'react'
import { facts, tailoring } from '../../api/endpoints'
import { ApiError, newIdempotencyKey } from '../../api/http'
import type {
  PlanBlock,
  SectionKind,
  TailorRun,
  TailorRunSummary,
  TailoredResumeData,
} from '../../api/types'
import { RUN_STATUS, SECTION, STOP_REASON } from '../../labels'
import {
  LIBRARY_QUERY,
  OVERVIEW_QUERY,
  TAILORED_RESUMES_QUERY,
  tailorResumeQuery,
  tailorRunQuery,
  tailorRunsQuery,
} from '../../queryKeys'
import { ResumeEvidence, SuggestAddList } from './ResumeEvidence'
import { ResumeHistory } from './ResumeHistory'

export const POLL_MS = 2000

export function ResumePanel({ jobId }: { jobId: string }): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const key = useRef(newIdempotencyKey())
  const [unsectioned, setUnsectioned] = useState<string[] | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedVersion, setSelectedVersion] = useState<number | undefined>(undefined)
  const [staleDismissed, setStaleDismissed] = useState(false)
  const [activeTab, setActiveTab] = useState('preview')

  const runs = useQuery({
    queryKey: tailorRunsQuery(jobId),
    queryFn: ({ signal }) => tailoring.runs(jobId, signal),
  })
  const activeSummary = runs.data?.find((item) => item.run_id === selectedRunId) ?? runs.data?.[0]

  const run = useQuery({
    queryKey: tailorRunQuery(activeSummary?.run_id ?? ''),
    queryFn: ({ signal }) => tailoring.run(activeSummary!.run_id, signal),
    enabled: Boolean(activeSummary),
    // 运行在后台执行：没结束就每两秒看一次，结束后停。
    refetchInterval: (query) => (query.state.data?.status === 'running' ? POLL_MS : false),
  })

  useEffect(() => {
    if (run.data && run.data.status !== 'running' && activeSummary?.status === 'running') {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: tailorRunsQuery(jobId) }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
        queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
      ])
    }
  }, [run.data, activeSummary?.status, jobId, queryClient])

  const start = useMutation({
    mutationFn: (sections: Record<string, SectionKind>) =>
      tailoring.start(jobId, sections, key.current),
    onSuccess: async () => {
      key.current = newIdempotencyKey() // 下次「重新生成」是新的一次
      setUnsectioned(null)
      setSelectedRunId(null)
      setSelectedVersion(undefined)
      setStaleDismissed(false)
      setActiveTab('preview')
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: tailorRunsQuery(jobId) }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
        queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
      ])
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
      type={activeSummary ? 'default' : 'primary'}
      icon={activeSummary ? <ReloadOutlined /> : undefined}
      loading={start.isPending}
      disabled={run.data?.status === 'running'}
      onClick={() => start.mutate({})}
    >
      {activeSummary ? '重新生成' : '生成定制简历'}
    </Button>
  )

  return (
    <Card title="定制简历" extra={generate} className="resume-panel">
      {runs.isPending ? (
        <Skeleton active />
      ) : runs.isError ? (
        <Empty description={runs.error.message}>
          <Button onClick={() => void runs.refetch()}>重试</Button>
        </Empty>
      ) : !activeSummary ? (
        <Empty description="按这个岗位改写你的经历：每一条都引用已确认的事实，检查不过的会退回原文。">
          <Button type="primary" loading={start.isPending} onClick={() => start.mutate({})}>
            生成定制简历
          </Button>
        </Empty>
      ) : run.isPending ? (
        <Skeleton active />
      ) : run.isError ? (
        <Empty description={run.error.message}>
          <Button onClick={() => void run.refetch()}>重试</Button>
        </Empty>
      ) : (
        <RunView
          run={run.data}
          runs={runs.data}
          hasResume={activeSummary.has_resume || run.data.status === 'succeeded'}
          selectedVersion={selectedVersion}
          staleDismissed={staleDismissed}
          activeTab={activeTab}
          regenerating={start.isPending}
          onTabChange={(tab) => {
            setActiveTab(tab)
            if (tab === 'evidence' || tab === 'history') {
              void queryClient.invalidateQueries({
                queryKey: tailorResumeQuery(run.data.run_id, selectedVersion),
              })
            }
          }}
          onPreviewVersion={(version) => {
            setSelectedVersion(version)
            setActiveTab('preview')
          }}
          onSelectRun={(runId) => {
            setSelectedRunId(runId)
            setSelectedVersion(undefined)
            setStaleDismissed(false)
          }}
          onDismissStale={() => setStaleDismissed(true)}
          onRegenerate={() => start.mutate({})}
        />
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

interface RunViewProps {
  run: TailorRun
  runs: TailorRunSummary[]
  hasResume: boolean
  selectedVersion: number | undefined
  staleDismissed: boolean
  activeTab: string
  regenerating: boolean
  onTabChange: (tab: string) => void
  onPreviewVersion: (version: number | undefined) => void
  onSelectRun: (runId: string) => void
  onDismissStale: () => void
  onRegenerate: () => void
}

function RunView({
  run,
  runs,
  hasResume,
  selectedVersion,
  staleDismissed,
  activeTab,
  regenerating,
  onTabChange,
  onPreviewVersion,
  onSelectRun,
  onDismissStale,
  onRegenerate,
}: RunViewProps): ReactElement {
  const queryClient = useQueryClient()
  const library = useQuery({
    queryKey: LIBRARY_QUERY,
    queryFn: ({ signal }) => facts.library(signal),
    enabled: hasResume,
  })
  const resume = useQuery({
    queryKey: tailorResumeQuery(run.run_id, selectedVersion),
    queryFn: ({ signal }) => tailoring.resume(run.run_id, selectedVersion, signal),
    enabled: run.status === 'succeeded' && hasResume,
  })

  if (run.status === 'running') {
    return <Progress blocks={run.blocks} startedAt={run.started_at} />
  }

  if (run.status === 'succeeded' && hasResume) {
    if (resume.isPending) return <Skeleton active />
    if (resume.isError) {
      return (
        <Empty description={resume.error.message}>
          <Button onClick={() => void resume.refetch()}>重试</Button>
        </Empty>
      )
    }
    const latestSet = library.data?.fact_set
    const isStale = Boolean(
      !staleDismissed && latestSet && run.fact_set_id && latestSet.fact_set_id !== run.fact_set_id,
    )
    const suggestCount = run.blocks.reduce(
      (sum, block) => sum + block.focus.filter((item) => item.base_status === 'suggest_add').length,
      0,
    )

    return (
      <Space orientation="vertical" size={12} className="full-width">
        <GuaranteeBanner resume={resume.data} onShowEvidence={() => onTabChange('evidence')} />
        {isStale && latestSet && (
          <Alert
            type="warning"
            showIcon
            title={`事实库已更新到 v${latestSet.version}，这份简历基于旧版事实生成`}
            action={
              <Space size={6}>
                <Button size="small" onClick={onDismissStale}>
                  先不用
                </Button>
                <Button size="small" loading={regenerating} onClick={onRegenerate}>
                  重新生成
                </Button>
              </Space>
            }
          />
        )}
        <Tabs
          activeKey={activeTab}
          onChange={onTabChange}
          items={[
            {
              key: 'preview',
              label: '预览与编辑',
              children: (
                <Preview
                  runId={run.run_id}
                  version={selectedVersion}
                  onBackToCurrent={() => onPreviewVersion(undefined)}
                />
              ),
            },
            {
              key: 'evidence',
              label: '依据追溯',
              children: <ResumeEvidence resume={resume.data} />,
            },
            {
              key: 'suggest',
              label: suggestCount > 0 ? `建议补入 (${suggestCount})` : '建议补入',
              children: <SuggestAddList blocks={run.blocks} />,
            },
            {
              key: 'history',
              label: '版本与运行历史',
              children: (
                <ResumeHistory
                  runId={run.run_id}
                  versions={resume.data.versions}
                  currentVersionId={resume.data.current_version_id}
                  selectedVersion={selectedVersion}
                  runs={runs}
                  onPreviewVersion={onPreviewVersion}
                  onRefreshVersions={() =>
                    void queryClient.invalidateQueries({
                      queryKey: tailorResumeQuery(run.run_id, selectedVersion),
                    })
                  }
                  onSelectRun={onSelectRun}
                />
              ),
            },
          ]}
        />
      </Space>
    )
  }

  return (
    <Space orientation="vertical" size={16} className="full-width">
      <Alert
        type={run.status === 'budget_exhausted' ? 'warning' : 'error'}
        showIcon
        title={RUN_STATUS[run.status]}
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
      {runs.length > 1 && (
        <ResumeHistory
          runId={run.run_id}
          versions={undefined}
          currentVersionId={undefined}
          selectedVersion={undefined}
          runs={runs}
          onPreviewVersion={onPreviewVersion}
          onRefreshVersions={() => undefined}
          onSelectRun={onSelectRun}
        />
      )}
    </Space>
  )
}

function GuaranteeBanner({
  resume,
  onShowEvidence,
}: {
  resume: TailoredResumeData
  onShowEvidence: () => void
}): ReactElement | null {
  const { token } = theme.useToken()
  const bullets = resume.content.sections.flatMap((s) => s.entries.flatMap((e) => e.bullets))
  if (bullets.length === 0) return null
  const fallback = bullets.filter((b) => b.origin === 'fallback').length
  const pending = bullets.filter((b) => b.origin === 'pending').length
  const passed = bullets.length - fallback - pending
  return (
    <div
      className="resume-guarantee"
      style={{ background: token.colorPrimaryBg, color: token.colorPrimaryText }}
    >
      <span className="tabular-num">
        改写 {bullets.length} 条 · 通过 {passed} 条 · 回退原文 {fallback} 条
        {pending > 0 ? ` · 待处理 ${pending} 条` : ''}
      </span>
      <Button size="small" onClick={onShowEvidence}>
        查看每条依据
      </Button>
    </div>
  )
}

function Progress({ blocks, startedAt }: { blocks: PlanBlock[]; startedAt: string }): ReactElement {
  const [elapsed, setElapsed] = useState(() =>
    Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000)),
  )
  useEffect(() => {
    const timer = window.setInterval(() => {
      setElapsed(Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000)))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [startedAt])

  const doneCount = blocks.filter((b) => b.status === 'done').length
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
      <Typography.Text type="secondary" className="tabular-num">
        已等待 {elapsed} 秒{blocks.length > 0 ? ` · 已完成 ${doneCount}/${blocks.length} 块` : ''}
        ，通常需要半分钟到一分钟，可以先离开，回来再看。
      </Typography.Text>
    </div>
  )
}

function Preview({
  runId,
  version,
  onBackToCurrent,
}: {
  runId: string
  version: number | undefined
  onBackToCurrent: () => void
}): ReactElement {
  const url = tailoring.previewUrl(runId, version)
  const readonly = version !== undefined
  return (
    <div className="resume-preview">
      {readonly && (
        <Alert
          type="info"
          showIcon
          className="resume-readonly-alert"
          title={`正在只读预览第 ${version} 版`}
          action={
            <Button size="small" onClick={onBackToCurrent}>
              回到当前版本
            </Button>
          }
        />
      )}
      <div className="resume-preview-actions">
        <Typography.Text type="secondary">
          {readonly ? '历史版本为只读，支持导出。' : '点「编辑」直接修改，自动保存。'}
        </Typography.Text>
        <Button icon={<ExpandOutlined />} href={url} target="_blank">
          {readonly ? '新窗口查看' : '全屏编辑'}
        </Button>
        <Dropdown
          menu={{
            items: [
              {
                key: 'html',
                label: <a href={tailoring.exportUrl(runId, 'html', version)}>HTML（可继续编辑）</a>,
              },
              {
                key: 'docx',
                label: <a href={tailoring.exportUrl(runId, 'docx', version)}>Word（DOCX）</a>,
              },
              {
                key: 'md',
                label: <a href={tailoring.exportUrl(runId, 'md', version)}>Markdown</a>,
              },
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
