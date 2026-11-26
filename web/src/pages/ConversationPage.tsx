// 对话页：新对话显示欢迎页，发出第一条消息时才建对话；已有对话左边是消息和输入框，
// 右边是当前岗位的产物面板。运行控制在 conversation/useConversationRun。
import { Prompts, Welcome } from '@ant-design/x'
import { App, Button, Drawer, Grid, Skeleton, Typography, theme } from 'antd'
import { useEffect, useRef, useState, type ReactElement, type ReactNode } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router'
import { conversations } from '../api/endpoints'
import { EmptyHint } from '../components/EmptyHint'
import { ArtifactPanel } from './conversation/ArtifactPanel'
import { Composer, type Draft } from './conversation/Composer'
import { JobTag } from './conversation/JobTag'
import { MessageList } from './conversation/MessageList'
import { titleFrom, useJobTitle } from './conversation/naming'
import type { PanelTab } from './conversation/panelTabs'
import type { LiveInput } from './conversation/runControl'
import { useConversationRun } from './conversation/useConversationRun'

const EMPTY_DRAFT: Draft = { text: '', attachments: [] }

const SUGGESTIONS = [
  {
    key: 'resume',
    label: '上传简历',
    description: '整理成经历事实',
    text: '这是我的简历，帮我整理一下经历：\n',
  },
  {
    key: 'jd',
    label: '发 JD 或截图',
    description: '存进岗位库并做匹配',
    text: '帮我看看这个岗位，JD 如下：\n',
  },
  {
    key: 'library',
    label: '从岗位库选一个',
    description: '接着准备已有岗位',
    text: '从我的岗位库里选一个最合适的岗位，帮我做匹配。',
  },
  {
    key: 'facts',
    label: '补充经历',
    description: '补上简历里没写的',
    text: '我想补充一段经历：\n',
  },
]

interface PendingState {
  pendingMessage?: LiveInput
}

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
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const [creating, setCreating] = useState(false)

  async function start(): Promise<void> {
    const input = inputOf(draft)
    setCreating(true)
    try {
      const created = await conversations.create(titleFrom(input))
      // 交给对话页去发：发消息用的 ID 跟着走，刷新后重发也只会接上同一轮。
      void navigate(`/conversations/${created.conversation_id}`, {
        state: { pendingMessage: input } satisfies PendingState,
      })
    } catch (error) {
      setCreating(false)
      void message.error(error instanceof Error ? error.message : '没能新建对话')
    }
  }

  return (
    <section className="conversation-page">
      <Heading title="新对话" />
      <div className="conversation-body is-welcome">
        <div className="conversation-welcome">
          <Welcome
            variant="borderless"
            title="今天想准备哪个岗位？"
            description="提供简历、JD 或补充经历，我只用你确认过的事实来写简历。"
          />
          <Prompts
            className="welcome-prompts"
            wrap
            items={SUGGESTIONS.map(({ key, label, description }) => ({ key, label, description }))}
            onItemClick={({ data }) => {
              const text = SUGGESTIONS.find((item) => item.key === data.key)?.text ?? ''
              setDraft((current) => ({ ...current, text }))
            }}
          />
        </div>
      </div>
      <Composer
        draft={draft}
        onChange={setDraft}
        onSubmit={() => void start()}
        blockedReason={creating ? '正在新建对话…' : null}
      />
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
  const pending = useRef((location.state as PendingState | null)?.pendingMessage ?? null)

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
    pending.current = null
    void navigate('.', { replace: true, state: null })
    void submit(input)
    // 只在进入页面时发一次从新对话页带来的消息。
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
          {jobId && (isNarrow || !panel.open) && (
            <Button size="small" className="view-result" onClick={() => panel.show()}>
              查看结果
            </Button>
          )}
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
