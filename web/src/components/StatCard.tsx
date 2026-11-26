// 概览数字卡：标签、大号数字加单位说明，可选一段补充说明和一个操作按钮。
// 工作台的状态卡和「我的」的概览卡共用，保证两页的数字卡长得一样。
import { Card, Typography } from 'antd'
import type { ReactElement, ReactNode } from 'react'

interface StatCardProps {
  label: ReactNode
  /** 大号数字；还没有数据时传 undefined，只显示 unit */
  value?: ReactNode
  unit?: ReactNode
  note?: ReactNode
  action?: ReactNode
}

export function StatCard({ label, value, unit, note, action }: StatCardProps): ReactElement {
  return (
    <Card size="small" className="stat-card">
      <Typography.Text type="secondary" className="stat-card-label">
        {label}
      </Typography.Text>
      <div className="stat-card-metric">
        {value !== undefined && <span>{value}</span>}
        {unit && (
          <Typography.Text type="secondary" className="stat-card-unit">
            {unit}
          </Typography.Text>
        )}
      </div>
      {note && (
        <Typography.Paragraph type="secondary" className="stat-card-note">
          {note}
        </Typography.Paragraph>
      )}
      {action && <div className="stat-card-action">{action}</div>}
    </Card>
  )
}
