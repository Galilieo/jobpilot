// 对话页：新对话显示欢迎页，发出第一条消息时才建对话；已有对话显示消息、卡片和输入框。
// 运行控制在 conversation/useConversationRun，消息渲染在 conversation/MessageList。
import { Prompts, Welcome } from '@ant-design/x'
import { App, Button, Skeleton, Typography, theme } from 'antd'
import { useEffect, useRef, useState, type ReactElement } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router'
import { conversations } from '../api/endpoints'
import { EmptyHint } from '../components/EmptyHint'
import { Composer } from './conversation/Composer'
import { titleFrom, useJobTitle } from './conversation/naming'
import { MessageList } from './conversation/MessageList'
import type { LiveInput } from './conversation/runControl'
import { useConversationRun } from './conversation/useConversationRun'

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

function newInput(content: string): LiveInput {
  return { clientMessageId: crypto.randomUUID(), content: content.trim(), attachments: [] }
}

function Heading({ title, extra }: { title: string; extra?: ReactElement }): ReactElement {
  const { token } = theme.useToken()
  return (
    <header
      className="conversation-heading"
      style={{ borderBottom: `1px solid ${token.colorBorderSecondary}` }}
    >
      <Typography.Text strong ellipsis className="conversation-title">
        {title}
      </Typography.Text>
      {extra}
    </header>
  )
}

function NewConversation(): ReactElement {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [text, setText] = useState('')
  const [creating, setCreating] = useState(false)

  async function start(): Promise<void> {
    const input = newInput(text)
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
              setText(SUGGESTIONS.find((item) => item.key === data.key)?.text ?? '')
            }}
          />
        </div>
      </div>
      <Composer
        value={text}
        onChange={setText}
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

function ConversationView({ conversationId }: { conversationId: string }): ReactElement {
  const { message } = App.useApp()
  const location = useLocation()
  const navigate = useNavigate()
  const run = useConversationRun(conversationId)
  const title = useJobTitle()
  const [text, setText] = useState('')
  const pending = useRef((location.state as PendingState | null)?.pendingMessage ?? null)

  async function submit(input: LiveInput): Promise<void> {
    setText('')
    const outcome = await run.send(input)
    if (!outcome.saved) {
      setText(input.content)
      void message.warning(outcome.reason)
    }
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
  if (detail.isPending) {
    return (
      <section className="conversation-page">
        <Heading title="对话" />
        <div className="conversation-body">
          <Skeleton active />
        </div>
      </section>
    )
  }
  if (detail.isError) {
    return (
      <section className="conversation-page">
        <Heading title="对话" />
        <div className="conversation-body">
          <EmptyHint
            description={detail.error.message}
            action={<Button onClick={() => void detail.refetch()}>重新加载</Button>}
          />
        </div>
      </section>
    )
  }

  const blockedReason = blockedReasonOf(run)
  const backgroundRunning = !run.streaming && blockedReason !== null && !detail.data.pending_action
  return (
    <section className="conversation-page">
      <Heading title={detail.data.title} />
      <div className="conversation-body">
        <div className="conversation-thread">
          <MessageList
            detail={detail.data}
            live={run.live}
            title={title}
            decide={run.decide}
            refresh={run.refresh}
            localHints={[]}
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
        value={text}
        onChange={setText}
        onSubmit={() => void submit(newInput(text))}
        blockedReason={blockedReason}
      />
    </section>
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
