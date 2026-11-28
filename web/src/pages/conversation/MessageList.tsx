// 对话的消息列表：详情里已保存的内容在前，正在流式进行的一轮接在后面。
// 卡片是待决定、执行中还是已有结果，全部按详情的 pending_action / latest_decided_action /
// confirmation_status 判断，前端不自己猜。
import { FileTextOutlined } from '@ant-design/icons'
import { Bubble } from '@ant-design/x'
import type { BubbleItemType } from '@ant-design/x'
import type { BubbleListRef } from '@ant-design/x/es/bubble'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, App } from 'antd'
import { useEffect, useRef, type ReactElement, type ReactNode } from 'react'
import { conversations } from '../../api/endpoints'
import type {
  AgentActionCard,
  AgentDecision,
  AgentTextAttachment,
  ConversationDetail,
} from '../../api/types'
import { MEMORIES_QUERY, WRITING_PREFERENCE_QUERY } from '../../queryKeys'
import { CardResult, PendingCard, type CardOutcome } from './ActionCards'
import { MessageText } from './MessageText'
import type { LiveTurn } from './runControl'
import { buildTimeline, hasCard, type TimelineItem } from './timeline'
import { ToolSteps, type RecordControls, type StepView } from './ToolSteps'

type JobTitle = (jobId: string) => string

interface MessageListProps {
  detail: ConversationDetail
  live: LiveTurn | null
  title: JobTitle
  decide: (actionId: string, decision: AgentDecision) => Promise<string | null>
  refresh: () => Promise<unknown>
  localHints: { key: string; text: string }[]
}

function cardOutcome(
  card: AgentActionCard,
  detail: ConversationDetail,
  past: Extract<TimelineItem, { type: 'card' }>['outcome'],
): CardOutcome | 'pending' {
  if (detail.pending_action?.action_id === card.action_id) return 'pending'
  if (detail.latest_decided_action?.action_id === card.action_id) {
    const status = detail.confirmation_status
    return status === 'decision_saved' || status === 'recovering'
      ? { state: 'saving' }
      : { state: 'decided', action: detail.latest_decided_action }
  }
  return { state: 'past', step: past }
}

function Attachments({ items }: { items: AgentTextAttachment[] }): ReactNode {
  if (items.length === 0) return null
  return (
    <div className="message-attachments">
      {items.map((item, index) => (
        <span key={index} className="attachment-chip">
          <FileTextOutlined /> {item.name ?? `附件 ${index + 1}`}
        </span>
      ))}
    </div>
  )
}

function userBubble(key: string, content: string, attachments: AgentTextAttachment[]) {
  return {
    key,
    role: 'user',
    content: (
      <>
        {content && <div className="message-text">{content}</div>}
        <Attachments items={attachments} />
      </>
    ),
  }
}

export function MessageList(props: MessageListProps): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { detail, live, title } = props
  const listRef = useRef<BubbleListRef>(null)
  const liveStarted = live !== null

  // 自己刚发出消息或提交决定时滚到底部；只在已经贴底时自动跟随是 Bubble.List 的默认行为。
  // 滚动容器在 Bubble.List 首次渲染之后才就绪，挂载时这一轮已开始（新对话发出第一条后跳转过来）
  // 不能滚动，否则会抛错；刚挂载的列表本来就停在底部。
  useEffect(() => {
    const list = listRef.current
    if (liveStarted && list?.scrollBoxNativeElement) list.scrollTo({ top: 'bottom' })
  }, [liveStarted])

  const undo = useMutation({
    mutationFn: (actionId: string) => conversations.undo(actionId),
    onSuccess: async () => {
      message.success('已撤销')
      await Promise.all([
        props.refresh(),
        queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY }),
        queryClient.invalidateQueries({ queryKey: WRITING_PREFERENCE_QUERY }),
      ])
    },
    // 例如「简历版本已变更，无法撤销」：之后又改过，后端拒绝撤销。
    onError: (error) => void message.error(error.message),
  })

  const recordControls = {
    undone: detail.undone_record_actions ?? [],
    undoing: undo.isPending ? undo.variables : undefined,
    onUndo: (id: string) => undo.mutate(id),
  }

  function cardBubble(card: AgentActionCard, outcome: CardOutcome | 'pending'): ReactNode {
    if (outcome === 'pending') {
      return (
        <PendingCard
          key={card.action_id}
          card={detail.pending_action ?? card}
          title={title}
          decide={(decision) => props.decide(card.action_id, decision)}
        />
      )
    }
    return (
      <CardResult
        tool={card.tool}
        outcome={outcome}
        title={title}
        undoing={undo.isPending && undo.variables === card.action_id}
        onUndo={() => undo.mutate(card.action_id)}
      />
    )
  }

  function itemOf(item: TimelineItem): BubbleItemType {
    switch (item.type) {
      case 'user':
        return userBubble(item.key, item.content, item.attachments)
      case 'assistant':
        return {
          key: item.key,
          role: 'ai',
          content: <MessageText text={item.content} />,
        }
      case 'steps':
        return {
          key: item.key,
          role: 'ai',
          content: (
            <ToolSteps
              title={title}
              records={recordControls}
              steps={item.steps.map((step, index): StepView => ({
                key: `${item.key}:${index}`,
                tool: step.tool,
                status: step.error ? 'error' : 'success',
                result: step,
                durationMs: typeof step.duration_ms === 'number' ? step.duration_ms : undefined,
              }))}
            />
          ),
        }
      case 'card':
        return {
          key: item.key,
          role: 'ai',
          content: cardBubble(item.card, cardOutcome(item.card, detail, item.outcome)),
        }
      case 'hint':
        return { key: item.key, role: 'system', content: item.text }
    }
  }

  const items: BubbleItemType[] = buildTimeline(detail, title).map(itemOf)
  for (const hint of props.localHints)
    items.push({ key: hint.key, role: 'system', content: hint.text })
  if (live) items.push(...liveItems(live, detail, title, cardBubble, recordControls))
  return <Bubble.List ref={listRef} className="message-list" items={items} role={ROLES} />
}

function liveItems(
  live: LiveTurn,
  detail: ConversationDetail,
  title: JobTitle,
  cardBubble: (card: AgentActionCard, outcome: 'pending') => ReactNode,
  recordControls: RecordControls,
): BubbleItemType[] {
  const items: BubbleItemType[] = []
  const { input } = live
  if (input && !detail.messages.some((m) => m.client_message_id === input.clientMessageId)) {
    items.push(userBubble(`live:${input.clientMessageId}`, input.content, input.attachments))
  }
  if (live.steps.length > 0) {
    items.push({
      key: 'live:steps',
      role: 'ai',
      content: (
        <ToolSteps
          title={title}
          records={recordControls}
          steps={live.steps.map((step) => ({ ...step, key: step.id }))}
        />
      ),
    })
  }
  if (live.card && !hasCard(detail, live.card.action_id)) {
    items.push({
      key: `live:${live.card.action_id}`,
      role: 'ai',
      content: cardBubble(live.card, 'pending'),
    })
  }
  if (live.error) {
    items.push({
      key: 'live:error',
      role: 'ai',
      content: <Alert type="warning" showIcon title={live.error} />,
    })
  } else if (live.text) {
    items.push({
      key: 'live:text',
      role: 'ai',
      streaming: true,
      content: <MessageText text={live.text} />,
    })
  } else if (!live.card && !live.steps.some((step) => step.status === 'loading')) {
    items.push({ key: 'live:loading', role: 'ai', loading: true, content: '' })
  }
  return items
}

const ROLES = {
  user: { placement: 'end' as const, variant: 'filled' as const, className: 'bubble-user' },
  ai: { placement: 'start' as const, variant: 'borderless' as const, className: 'bubble-ai' },
}
