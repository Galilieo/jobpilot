// 极简空状态：一句说明文字加可选的下一步按钮，不使用带默认插图的 Empty 组件。
import { Typography } from 'antd'
import type { ReactElement, ReactNode } from 'react'

interface EmptyHintProps {
  description: ReactNode
  action?: ReactNode
  compact?: boolean
}

export function EmptyHint({ description, action, compact }: EmptyHintProps): ReactElement {
  return (
    <div className={compact ? 'empty-hint empty-hint-compact' : 'empty-hint'}>
      <Typography.Text type="secondary" className="empty-hint-text">
        {description}
      </Typography.Text>
      {action}
    </div>
  )
}
