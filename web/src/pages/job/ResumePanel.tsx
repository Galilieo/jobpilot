// 定制简历面板：单层主体（顶部工具栏、保证条与可编辑预览），把依据追溯、底稿没写到与历史收进「更多」抽屉。
import { ReloadOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Drawer,
  Dropdown,
  Form,
  Modal,
  Select,
  Skeleton,
  Space,
  Steps,
  Switch,
  Typography,
} from 'antd'
import type { MenuProps } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import { facts, tailoring } from '../../api/endpoints'
import { ApiError, newIdempotencyKey } from '../../api/http'
import type {
  PlanBlock,
  SectionKind,
  TailorRun,
  TailorRunSummary,
  TailoredResumeData,
} from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { indexResumeFacts } from '../../factIndex'
import { RUN_STATUS, SECTION, STOP_REASON } from '../../labels'
import {
  LIBRARY_QUERY,
  OVERVIEW_QUERY,
  TAILORED_RESUMES_QUERY,
  tailorResumeQuery,
  tailorRunQuery,
  tailorRunsQuery,
} from '../../queryKeys'
import { ResumeEvidence, UnwrittenRequirements } from './ResumeEvidence'
import { ResumeHistory } from './ResumeHistory'
import { countUnwritten } from './unwritten'

export const POLL_MS = 2000

type DrawerKind = 'evidence' | 'unwritten' | 'history' | null

interface FitState {
  fit: boolean
  percent: number
  overflowPages: number
  text: string
}

export function ResumePanel({
  jobId,
  embedded = false,
}: {
  jobId: string
  embedded?: boolean
}): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const key = useRef(newIdempotencyKey())
  const [unsectioned, setUnsectioned] = useState<string[] | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedVersion, setSelectedVersion] = useState<number | undefined>(undefined)
  const [staleDismissed, setStaleDismissed] = useState(false)

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
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: tailorRunsQuery(jobId) }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
        queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
      ])
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 410) key.current = newIdempotencyKey()
      const detail = error instanceof ApiError ? error.detail : null
      if (detail && typeof detail === 'object' && 'experiences' in detail) {
        setUnsectioned(detail.experiences as string[]) // 让用户指定栏目后重试
        return
      }
      message.error(error.message)
    },
  })

  const regenerate = activeSummary ? (
    <Button
      icon={<ReloadOutlined />}
      loading={start.isPending}
      disabled={run.data?.status === 'running'}
      onClick={() => start.mutate({})}
    >
      重新生成
    </Button>
  ) : null

  const body = (
    <>
      {runs.isPending ? (
        <Skeleton active />
      ) : runs.isError ? (
        <EmptyHint
          description={runs.error.message}
          action={<Button onClick={() => void runs.refetch()}>重试</Button>}
        />
      ) : !activeSummary ? (
        <EmptyHint
          description="按这个岗位改写你的经历：每一条都引用已确认的事实，检查不过的会退回原文。"
          action={
            <Button type="primary" loading={start.isPending} onClick={() => start.mutate({})}>
              生成定制简历
            </Button>
          }
        />
      ) : run.isPending ? (
        <Skeleton active />
      ) : run.isError ? (
        <EmptyHint
          description={run.error.message}
          action={<Button onClick={() => void run.refetch()}>重试</Button>}
        />
      ) : (
        <RunView
          run={run.data}
          runs={runs.data}
          hasResume={activeSummary.has_resume || run.data.status === 'succeeded'}
          selectedVersion={selectedVersion}
          staleDismissed={staleDismissed}
          regenerating={start.isPending}
          onPreviewVersion={setSelectedVersion}
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
    </>
  )

  if (embedded) return <div className="resume-panel-embedded">{body}</div>

  return (
    <Card title="定制简历" extra={regenerate} className="resume-panel">
      {body}
    </Card>
  )
}

interface RunViewProps {
  run: TailorRun
  runs: TailorRunSummary[]
  hasResume: boolean
  selectedVersion: number | undefined
  staleDismissed: boolean
  regenerating: boolean
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
  regenerating,
  onPreviewVersion,
  onSelectRun,
  onDismissStale,
  onRegenerate,
}: RunViewProps): ReactElement {
  const queryClient = useQueryClient()
  const [showEvidence, setShowEvidence] = useState(true)
  const [fitInfo, setFitInfo] = useState<FitState | null>(null)
  const [drawer, setDrawer] = useState<DrawerKind>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)

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
  const resumesList = useQuery({
    queryKey: TAILORED_RESUMES_QUERY,
    queryFn: ({ signal }) => tailoring.listAll(signal),
    enabled: run.status === 'succeeded' && hasResume,
  })

  const factIndex = useMemo(
    () => (resume.data ? indexResumeFacts(resume.data) : null),
    [resume.data],
  )

  const syncEvidence = useCallback(() => {
    const win = iframeRef.current?.contentWindow
    if (!win || !resume.data || !factIndex) return
    const changedIds = resume.data.content.sections.flatMap((section) =>
      section.entries.flatMap((entry) =>
        entry.bullets.filter((bullet) => bullet.edited).map((bullet) => bullet.bullet_id),
      ),
    )
    win.postMessage(
      {
        type: 'jobpilot:evidence',
        show: showEvidence,
        bullets: factIndex.toEditorPayload(),
        changedIds,
      },
      window.location.origin,
    )
  }, [factIndex, resume.data, showEvidence])

  useEffect(() => {
    syncEvidence()
  }, [syncEvidence])

  useEffect(() => {
    function onMessage(event: MessageEvent): void {
      if (event.origin !== window.location.origin) return
      const payload = event.data as Record<string, unknown> | null
      if (!payload || typeof payload !== 'object') return
      if (payload.type === 'jobpilot:ready') {
        syncEvidence()
      } else if (payload.type === 'jobpilot:fit') {
        setFitInfo({
          fit: Boolean(payload.fit),
          percent: Number(payload.percent ?? 0),
          overflowPages: Number(payload.overflowPages ?? 0),
          text: String(payload.text ?? ''),
        })
      } else if (payload.type === 'jobpilot:saved') {
        void Promise.all([
          queryClient.invalidateQueries({
            queryKey: tailorResumeQuery(run.run_id, selectedVersion),
          }),
          queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
        ])
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [queryClient, run.run_id, selectedVersion, syncEvidence])

  if (run.status === 'running') {
    return <Progress blocks={run.blocks} startedAt={run.started_at} />
  }

  if (run.status === 'succeeded' && hasResume) {
    if (resume.isPending) return <Skeleton active />
    if (resume.isError) {
      return (
        <EmptyHint
          description={resume.error.message}
          action={<Button onClick={() => void resume.refetch()}>重试</Button>}
        />
      )
    }

    const latestSet = library.data?.fact_set ?? null
    const isStale = Boolean(
      !staleDismissed && latestSet && run.fact_set_id && latestSet.fact_set_id !== run.fact_set_id,
    )
    const runSummary = resumesList.data?.find((item) => item.run_id === run.run_id)
    const factVersion =
      runSummary?.fact_set_version ??
      (latestSet && latestSet.fact_set_id === run.fact_set_id ? latestSet.version : null)
    const unwrittenCount = countUnwritten(run.blocks)
    const url = tailoring.previewUrl(run.run_id, selectedVersion)
    const readonly = selectedVersion !== undefined

    function openDrawer(kind: Exclude<DrawerKind, null>): void {
      if (kind === 'evidence' || kind === 'history') {
        void queryClient.invalidateQueries({
          queryKey: tailorResumeQuery(run.run_id, selectedVersion),
        })
      }
      setDrawer(kind)
    }

    function exportPdf(): void {
      const win = iframeRef.current?.contentWindow
      if (win) {
        win.postMessage({ type: 'jobpilot:print' }, window.location.origin)
      } else {
        window.open(url, '_blank', 'noopener,noreferrer')
      }
    }

    const moreItems: MenuProps['items'] = [
      { key: 'evidence', label: '依据追溯', onClick: () => openDrawer('evidence') },
      {
        key: 'unwritten',
        label: unwrittenCount > 0 ? `底稿没写到 (${unwrittenCount})` : '底稿没写到',
        onClick: () => openDrawer('unwritten'),
      },
      { key: 'history', label: '版本与运行历史', onClick: () => openDrawer('history') },
      { type: 'divider' },
      {
        key: 'html',
        label: <a href={tailoring.exportUrl(run.run_id, 'html', selectedVersion)}>导出 HTML</a>,
      },
      {
        key: 'docx',
        label: (
          <a href={tailoring.exportUrl(run.run_id, 'docx', selectedVersion)}>导出 Word（DOCX）</a>
        ),
      },
      {
        key: 'md',
        label: <a href={tailoring.exportUrl(run.run_id, 'md', selectedVersion)}>导出 Markdown</a>,
      },
      { type: 'divider' },
      {
        key: 'fullscreen',
        label: (
          <a href={url} target="_blank" rel="noreferrer">
            {readonly ? '新窗口查看' : '全屏编辑'}
          </a>
        ),
      },
      {
        key: 'regenerate',
        label: '重新生成',
        disabled: regenerating,
        onClick: onRegenerate,
      },
    ]

    const drawerTitle =
      drawer === 'evidence'
        ? '依据追溯'
        : drawer === 'unwritten'
          ? '底稿没写到'
          : drawer === 'history'
            ? '版本与运行历史'
            : ''

    return (
      <div className="resume-view">
        <div className="resume-toolbar">
          <label className="resume-evidence-switch">
            <Switch
              size="small"
              checked={showEvidence}
              aria-label="显示依据"
              onChange={setShowEvidence}
            />
            <span>显示依据</span>
          </label>
          {fitInfo && (
            <span className="resume-fit tabular-num">
              {fitInfo.fit ? (
                <>
                  一页内，占用 <b>{fitInfo.percent}%</b>
                </>
              ) : (
                <>
                  超出 <b>{fitInfo.overflowPages}</b> 页
                </>
              )}
            </span>
          )}
          <div className="resume-toolbar-actions">
            <Dropdown trigger={['click']} menu={{ items: moreItems }}>
              <Button>更多</Button>
            </Dropdown>
            <Button type="primary" onClick={exportPdf}>
              导出 PDF
            </Button>
          </div>
        </div>

        {isStale && latestSet ? (
          <Alert
            type="warning"
            showIcon
            className="resume-stale-alert"
            title={`这份简历基于${factVersion ? `事实库 v${factVersion} ` : '旧版事实库'}生成，事实库已更新到 v${latestSet.version}`}
            action={
              <Space size={6}>
                <Button size="small" onClick={onDismissStale}>
                  先不用
                </Button>
                <Button size="small" type="primary" loading={regenerating} onClick={onRegenerate}>
                  重新生成
                </Button>
              </Space>
            }
          />
        ) : (
          <GuaranteeBanner resume={resume.data} factVersion={factVersion} />
        )}

        <div className="resume-paper-wrap">
          {readonly && (
            <Alert
              type="info"
              showIcon
              className="resume-readonly-alert"
              title={`正在只读预览第 ${selectedVersion} 版`}
              action={
                <Button size="small" onClick={() => onPreviewVersion(undefined)}>
                  回到当前版本
                </Button>
              }
            />
          )}
          <iframe
            ref={iframeRef}
            title="定制简历预览"
            src={url}
            className="resume-frame"
            onLoad={syncEvidence}
          />
        </div>

        <Drawer
          title={drawerTitle}
          open={drawer !== null}
          onClose={() => setDrawer(null)}
          size={480}
          destroyOnHidden
        >
          {drawer === 'evidence' && (
            <ResumeEvidence resume={resume.data} factIndex={factIndex ?? undefined} />
          )}
          {drawer === 'unwritten' && <UnwrittenRequirements blocks={run.blocks} />}
          {drawer === 'history' && (
            <ResumeHistory
              runId={run.run_id}
              versions={resume.data.versions}
              currentVersionId={resume.data.current_version_id}
              selectedVersion={selectedVersion}
              runs={runs}
              onPreviewVersion={(version) => {
                onPreviewVersion(version)
                setDrawer(null)
              }}
              onRefreshVersions={() =>
                void queryClient.invalidateQueries({
                  queryKey: tailorResumeQuery(run.run_id, selectedVersion),
                })
              }
              onSelectRun={(runId) => {
                onSelectRun(runId)
                setDrawer(null)
              }}
            />
          )}
        </Drawer>
      </div>
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
  factVersion,
}: {
  resume: TailoredResumeData
  factVersion: number | null
}): ReactElement | null {
  const bullets = resume.content.sections.flatMap((s) => s.entries.flatMap((e) => e.bullets))
  if (bullets.length === 0) return null
  const fallback = bullets.filter((b) => b.origin === 'fallback').length
  const pending = bullets.filter((b) => b.origin === 'pending').length
  const passed = bullets.length - fallback - pending

  const summary =
    fallback === 0 && pending === 0
      ? '每条都能追溯到你确认过的事实；没有条目回退到原文。'
      : `可追溯 ${passed} 条${fallback > 0 ? `，${fallback} 条回退到原文` : ''}${pending > 0 ? `，${pending} 条待处理` : ''}。`

  return (
    <div className="resume-guarantee">
      <SafetyCertificateOutlined className="resume-guarantee-icon" />
      <span className="resume-guarantee-copy tabular-num">
        <b>改写 {bullets.length} 条</b>，{summary}
      </span>
      {factVersion !== null && (
        <span className="resume-guarantee-version tabular-num">基于事实库 v{factVersion}</span>
      )}
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
