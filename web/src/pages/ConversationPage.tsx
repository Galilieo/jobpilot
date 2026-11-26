// 对话页：新对话显示欢迎页，发出第一条消息时才建对话；已有对话左边是消息和输入框，
// 右边是当前岗位的产物面板。运行控制在 conversation/useConversationRun。
import {
  CheckOutlined,
  EllipsisOutlined,
  PlusOutlined,
  SolutionOutlined,
  UnorderedListOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { App, Button, Drawer, Dropdown, Grid, Skeleton, Typography, theme } from 'antd'
import { useEffect, useRef, useState, type ReactElement, type ReactNode } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router'
import type { ConversationDetail } from '../api/types'
import { EmptyHint } from '../components/EmptyHint'
import { ArtifactPanel } from './conversation/ArtifactPanel'
import { CompactModal } from './conversation/CompactModal'
import { Composer, type Draft } from './conversation/Composer'
import { JobTag } from './conversation/JobTag'
import { MessageList } from './conversation/MessageList'
import { useJobTitle } from './conversation/naming'
import type { PanelTab } from './conversation/panelTabs'
import type { LiveInput } from './conversation/runControl'
import { useConversationRun } from './conversation/useConversationRun'
import {
  useStartConversation,
  type PendingConversationState,
} from './conversation/useStartConversation'

const EMPTY_DRAFT: Draft = { text: '', attachments: [] }

const SUGGESTIONS = [
  {
    key: 'resume',
    label: '上传简历',
    icon: <UploadOutlined />,
    text: '我上传一份简历，帮我整理成经历事实。',
  },
  {
    key: 'jd',
    label: '发 JD 或截图',
    icon: <SolutionOutlined />,
    text: '这是一个岗位的 JD：\n',
  },
  {
    key: 'library',
    label: '从岗位库选一个',
    icon: <UnorderedListOutlined />,
    text: '从岗位库里选一个岗位继续准备。',
  },
  {
    key: 'facts',
    label: '补充经历',
    icon: <PlusOutlined />,
    text: '我想补充一段简历里没写的经历：\n',
  },
]

const TRUST_ITEMS = ['只用你确认过的经历', '改动前先问你', '每条都能查到依据']

function inputOf(draft: Draft): LiveInput {
  return {
    clientMessageId: crypto.randomUUID(),
    content: draft.text.trim(),
    attachments: draft.attachments,
  }
}

function Heading({ title, children }: { title: string; children?: ReactNode }): ReactElement {
  const { token } = theme.useToken()
  return (
    <header
      className="conversation-heading"
      style={{ borderBottom: `1px solid ${token.colorBorderSecondary}` }}
    >
      <Typography.Text strong ellipsis className="conversation-title">
        {title}
      </Typography.Text>
      {children}
    </header>
  )
}

function NewConversation(): ReactElement {
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const { start, creating } = useStartConversation()
  const composerRef = useRef<HTMLDivElement>(null)

  function fillStarter(text: string): void {
    setDraft((current) => ({ ...current, text }))
    composerRef.current?.querySelector('textarea')?.focus()
  }

  return (
    <section className="conversation-page">
      <Heading title="新对话">
        <span className="job-pill-static is-empty">未关联岗位</span>
      </Heading>
      <div className="conversation-body is-welcome">
        <div className="conversation-welcome">
          <h2 className="welcome-heading serif-heading">今天想准备哪个岗位？</h2>
          <Typography.Paragraph type="secondary" className="welcome-subtitle">
            贴一段 JD、传一份简历，或者说说你想补充的经历。
          </Typography.Paragraph>
          <Composer
            variant="prominent"
            minRows={3}
            hideNote
            containerRef={composerRef}
            placeholder="例如：帮我看看这个岗位，存进岗位库后做匹配。（可以直接粘贴 JD）"
            draft={draft}
            onChange={setDraft}
            onSubmit={() => void start(inputOf(draft))}
            blockedReason={creating ? '正在新建对话…' : null}
          />
          <div className="welcome-starters">
            {SUGGESTIONS.map((item) => (
              <Button
                key={item.key}
                className="starter-btn"
                icon={item.icon}
                onClick={() => fillStarter(item.text)}
              >
                {item.label}
              </Button>
            ))}
          </div>
          <div className="welcome-trust">
            {TRUST_ITEMS.map((item) => (
              <span key={item} className="welcome-trust-item">
                <CheckOutlined className="welcome-trust-icon" />
                {item}
              </span>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}

function blockedReasonOf(run: ReturnType<typeof useConversationRun>): string | null {
  const data = run.detail.data
  if (run.streaming || data?.run?.status === 'running') return '这一轮还没结束，稍等一下…'
  if (data?.pending_action) return '先处理上面的卡片，再发新消息'
  if (data?.confirmation_status) return '正在执行你的决定…'
  return null
}

function writeKeyOf(detail: ConversationDetail): string {
  const action = detail.latest_decided_action
  return action ? `${action.action_id}:${action.status}` : 'none'
}

function usePanel(isNarrow: boolean) {
  const [tab, setTab] = useState<PanelTab | null>(null)
  // 宽屏默认打开，窄屏默认收起、由「查看结果」打开为全屏层。
  const [open, setOpen] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  return {
    tab,
    setTab,
    open: open ?? !isNarrow,
    setOpen,
    expanded,
    toggleExpanded: () => setExpanded((value) => !value),
    show: (next: PanelTab | null = null) => {
      if (next) setTab(next)
      setOpen(true)
    },
  }
}

function ConversationView({ conversationId }: { conversationId: string }): ReactElement {
  const { message } = App.useApp()
  const location = useLocation()
  const navigate = useNavigate()
  const isNarrow = Grid.useBreakpoint().lg === false
  const run = useConversationRun(conversationId)
  const title = useJobTitle()
  const panel = usePanel(isNarrow)
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const [hints, setHints] = useState<{ key: string; text: string }[]>([])
  const [compactOpen, setCompactOpen] = useState(false)
  const pending = useRef(
    (location.state as PendingConversationState | null)?.pendingMessage ?? null,
  )

  async function submit(input: LiveInput): Promise<void> {
    setDraft(EMPTY_DRAFT)
    const outcome = await run.send(input)
    if (!outcome.saved) {
      setDraft({ text: input.content, attachments: input.attachments })
      void message.warning(outcome.reason)
    }
  }

  async function jobChanged(text: string): Promise<void> {
    // 面板上改的绑定不会存成消息，只在这次打开的页面里加一行提示。
    setHints((current) => [...current, { key: `local:${current.length}`, text }])
    await run.refresh()
  }

  useEffect(() => {
    const input = pending.current
    if (!input) return
    void navigate('.', { replace: true, state: null })
    void submit(input)
    // 只在进入页面时发从新对话页带来的消息。不清掉 pending：开发模式下组件会卸载再挂载一次，
    // 第一次的流随卸载中止，第二次用同一个 client_message_id 重发，后端接上同一轮。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const { detail } = run
  if (detail.isPending || detail.isError) {
    return (
      <section className="conversation-page">
        <Heading title="对话" />
        <div className="conversation-body">
          {detail.isPending ? (
            <Skeleton active />
          ) : (
            <EmptyHint
              description={detail.error.message}
              action={<Button onClick={() => void detail.refetch()}>重新加载</Button>}
            />
          )}
        </div>
      </section>
    )
  }

  const jobId = detail.data.current_job_id
  const blockedReason = blockedReasonOf(run)
  const backgroundRunning = !run.streaming && blockedReason !== null && !detail.data.pending_action
  const panelVisible = jobId !== null && panel.open
  const panelContent = jobId && (
    <ArtifactPanel
      jobId={jobId}
      writeKey={writeKeyOf(detail.data)}
      activeTab={panel.tab}
      onTabChange={panel.setTab}
      expanded={!isNarrow && panel.expanded}
      onToggleExpand={isNarrow ? undefined : panel.toggleExpanded}
      onClose={isNarrow ? undefined : () => panel.setOpen(false)}
    />
  )

  const layoutClass = [
    'conversation-layout',
    panelVisible && !isNarrow ? 'has-panel' : '',
    panelVisible && panel.expanded && !isNarrow ? 'panel-expanded' : '',
  ].join(' ')

  return (
    <div className={layoutClass}>
      <section className="conversation-page">
        <Heading title={detail.data.title}>
          <JobTag
            conversationId={conversationId}
            currentJobId={jobId}
            title={title}
            onView={() => panel.show('job')}
            onChanged={jobChanged}
          />
          <div className="conversation-heading-extra">
            {jobId && (isNarrow || !panel.open) && (
              <Button size="small" className="view-result" onClick={() => panel.show()}>
                查看结果
              </Button>
            )}
            <Dropdown
              trigger={['click']}
              menu={{
                items: [{ key: 'compact', label: '压缩上下文' }],
                onClick: ({ key }) => {
                  if (key === 'compact') setCompactOpen(true)
                },
              }}
            >
              <Button size="small" type="text" icon={<EllipsisOutlined />} aria-label="更多" />
            </Dropdown>
          </div>
        </Heading>
        <div className="conversation-body">
          <div className="conversation-thread">
            <MessageList
              detail={detail.data}
              live={run.live}
              title={title}
              decide={run.decide}
              refresh={run.refresh}
              localHints={hints}
            />
            {backgroundRunning && (
              <Typography.Text type="secondary" className="background-run">
                Agent 还在后台处理，结果出来后会自动显示。
                <Typography.Link onClick={() => void run.refresh()}>立即刷新</Typography.Link>
              </Typography.Text>
            )}
          </div>
        </div>
        <Composer
          draft={draft}
          onChange={setDraft}
          onSubmit={() => void submit(inputOf(draft))}
          blockedReason={blockedReason}
        />
      </section>
      {panelVisible && !isNarrow && <aside className="artifact-panel">{panelContent}</aside>}
      {isNarrow && (
        <Drawer
          title="结果"
          placement="right"
          size="100%"
          open={panelVisible}
          onClose={() => panel.setOpen(false)}
          styles={{ body: { padding: 0 } }}
          destroyOnHidden
        >
          {panelContent}
        </Drawer>
      )}
      <CompactModal
        conversationId={conversationId}
        open={compactOpen}
        onClose={() => setCompactOpen(false)}
      />
    </div>
  )
}

export function ConversationPage(): ReactElement {
  const { conversationId } = useParams()
  // 换对话时整页重建，上一段对话进行中的流不会串到下一段。
  return conversationId ? (
    <ConversationView key={conversationId} conversationId={conversationId} />
  ) : (
    <NewConversation />
  )
}
