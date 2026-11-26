// 事实编号小块（F1、F2…）：悬停或键盘聚焦时用 Tooltip 显示「编号 + 事实原文」。
import { Tooltip } from 'antd'
import type { ReactElement } from 'react'

interface FactBadgeProps {
  code: string
  text: string
  variant?: 'default' | 'on-green'
}

export function FactBadge({ code, text, variant = 'default' }: FactBadgeProps): ReactElement {
  return (
    <Tooltip title={`${code} ${text}`} trigger={['hover', 'focus']}>
      <span
        className={
          variant === 'on-green' ? 'fact-badge is-on-green tabular-num' : 'fact-badge tabular-num'
        }
        tabIndex={0}
      >
        {code}
      </span>
    </Tooltip>
  )
}
