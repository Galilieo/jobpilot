// 输入框右下角的上下文用量小圆环：悬停看已用多少、什么时候自动压缩，点击手动压缩。
// 数字来自后端，和自动压缩用同一套估算，前端不自己数 token。
import { useQuery } from '@tanstack/react-query'
import { Progress, Tooltip } from 'antd'
import type { ReactElement } from 'react'
import { conversations } from '../../api/endpoints'
import type { ContextUsage } from '../../api/types'
import { contextUsageQuery } from '../../queryKeys'
import { formatTokens } from './naming'

interface ContextMeterProps {
  conversationId: string
  messageCount: number
  updatedAt: string
  onCompact: () => void
}

function percentOf(part: number, whole: number): number {
  return Math.min(100, Math.round((part / whole) * 100))
}

export function ContextMeterView({
  usage,
  onCompact,
}: {
  usage: ContextUsage
  onCompact: () => void
}): ReactElement {
  const percent = percentOf(usage.used_tokens, usage.window_tokens)
  const nearCompact = usage.used_tokens >= usage.compact_at_tokens
  const tip = (
    <div className="context-meter-tip">
      <div className="tabular-num">上下文已用 {percent}%</div>
      <div className="tabular-num">
        约 {formatTokens(usage.used_tokens)} / {formatTokens(usage.window_tokens)} token
      </div>
      <div>
        {nearCompact
          ? '这一轮结束后会自动压缩较早的对话'
          : `到 ${percentOf(usage.compact_at_tokens, usage.window_tokens)}% 时自动压缩较早的对话`}
      </div>
      <div>点击可手动压缩</div>
    </div>
  )
  return (
    <Tooltip title={tip} placement="topRight">
      <button
        type="button"
        className={nearCompact ? 'context-meter is-near' : 'context-meter'}
        aria-label={`上下文已用 ${percent}%，点击压缩`}
        onClick={onCompact}
      >
        <Progress
          type="circle"
          size={18}
          strokeWidth={14}
          percent={percent}
          showInfo={false}
          status={nearCompact ? 'exception' : 'normal'}
        />
      </button>
    </Tooltip>
  )
}

export function ContextMeter({
  conversationId,
  messageCount,
  updatedAt,
  onCompact,
}: ContextMeterProps): ReactElement | null {
  const usage = useQuery({
    queryKey: contextUsageQuery(conversationId, messageCount, updatedAt),
    queryFn: ({ signal }) => conversations.context(conversationId, signal),
    placeholderData: (previous) => previous,
  })
  return usage.data ? <ContextMeterView usage={usage.data} onCompact={onCompact} /> : null
}
