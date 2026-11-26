// 把详情里的消息整理成对话里一项项要显示的内容：用户消息、回复、工具步骤、卡片、岗位提示。
// 只做整理，不请求接口；卡片当前处于什么状态由 cardState 按详情判断。
import type {
  AgentActionCard,
  AgentTextAttachment,
  AgentToolStep,
  ConversationDetail,
  ConversationMessage,
} from '../../api/types'

export type ToolResultStep = Extract<AgentToolStep, { tool: string }>

export type TimelineItem =
  | { type: 'user'; key: string; content: string; attachments: AgentTextAttachment[] }
  | { type: 'assistant'; key: string; content: string }
  | { type: 'steps'; key: string; steps: ToolResultStep[] }
  | { type: 'card'; key: string; card: AgentActionCard; outcome: ToolResultStep | null }
  | { type: 'hint'; key: string; text: string }

type JobTitle = (jobId: string) => string

export function isCard(step: AgentToolStep): step is { confirm: AgentActionCard } {
  return 'confirm' in step
}

/** 绑定变化没有单独存成消息，按工具结果的先后推出来：解析 JD 时还没绑定才会自动绑定。 */
function bindingHint(step: ToolResultStep, bound: string | null, title: JobTitle) {
  if (step.error) return null
  const jobId = typeof step.job_id === 'string' ? step.job_id : null
  if (step.tool === 'parse_jd' && jobId) {
    return bound === null
      ? { bound: jobId, text: `已存入岗位库，并设为当前岗位：${title(jobId)}` }
      : { bound, text: `已存入岗位库：${title(jobId)}` }
  }
  if (step.tool === 'set_current_job' && 'job_id' in step) {
    return jobId
      ? { bound: jobId, text: `当前岗位已设为：${title(jobId)}` }
      : { bound: null, text: '已解除当前岗位' }
  }
  if (step.tool === 'ask_user' && step.decision === 'switch' && jobId) {
    return { bound: jobId, text: `当前岗位已切换为：${title(jobId)}` }
  }
  return null
}

/** 详情里是否已经有这张卡（出卡时会先存一条消息再推送）。 */
export function hasCard(detail: ConversationDetail, actionId: string): boolean {
  return detail.messages.some((message) =>
    message.steps_json.some((step) => isCard(step) && step.confirm.action_id === actionId),
  )
}

/** 卡片之后同一运行里第一个同名工具的结果，就是这张卡的执行结果。 */
function outcomeOf(card: AgentActionCard, messages: ConversationMessage[], from: number) {
  for (const message of messages.slice(from + 1)) {
    if (message.run_id !== card.run_id) break
    for (const step of message.steps_json) {
      if (!isCard(step) && step.tool === card.tool) return step
    }
  }
  return null
}

export function buildTimeline(detail: ConversationDetail, title: JobTitle): TimelineItem[] {
  const items: TimelineItem[] = []
  const messages = detail.messages
  let bound: string | null = null
  let group: { type: 'steps'; key: string; steps: ToolResultStep[] } | null = null
  // 岗位提示放在这一串工具步骤之后，不把步骤切成几段。
  let hints: TimelineItem[] = []

  function closeGroup(): void {
    items.push(...hints)
    hints = []
    group = null
  }

  messages.forEach((message, index) => {
    if (message.role === 'user') {
      closeGroup()
      items.push({
        type: 'user',
        key: message.message_id,
        content: message.content,
        attachments: message.attachments_json,
      })
      return
    }
    if (message.role === 'assistant') {
      closeGroup()
      if (message.content) {
        items.push({ type: 'assistant', key: message.message_id, content: message.content })
      }
      return
    }
    for (const [stepIndex, step] of message.steps_json.entries()) {
      const key = `${message.message_id}:${stepIndex}`
      if (isCard(step)) {
        closeGroup()
        const outcome = outcomeOf(step.confirm, messages, index)
        items.push({ type: 'card', key, card: step.confirm, outcome })
        continue
      }
      if (!group) {
        group = { type: 'steps', key, steps: [] }
        items.push(group)
      }
      group.steps.push(step)
      const hint = bindingHint(step, bound, title)
      if (hint) {
        bound = hint.bound
        hints.push({ type: 'hint', key: `${key}:hint`, text: hint.text })
      }
    }
  })
  closeGroup()
  return items
}
