// 投递卡片和列表共用的状态标记：没进展提示、岗位已删除、第 N 次投递、迁移前记录。
import type { ReactElement } from 'react'
import type { ApplicationListItem } from '../../api/types'
import { daysInCurrentStage, isStaleApplication, todayLocalDate } from './applicationStatus'

interface ApplicationChipsProps {
  record: ApplicationListItem
  attemptNumber?: number
  today?: string
  showFallbackDays?: boolean
}

export function ApplicationChips({
  record,
  attemptNumber,
  today = todayLocalDate(),
  showFallbackDays = false,
}: ApplicationChipsProps): ReactElement | null {
  const days = daysInCurrentStage(record.stage_changed_at, today)
  const chips: ReactElement[] = []

  if (isStaleApplication(record, today)) {
    chips.push(
      <span key="stale" className="app-chip is-stale tabular-num">
        {days} 天没有进展
      </span>,
    )
  }
  if (record.user_job_id === null) {
    chips.push(
      <span key="gone" className="app-chip is-gone">
        岗位已删除
      </span>,
    )
  }
  if (attemptNumber !== undefined) {
    chips.push(
      <span key="again" className="app-chip is-again tabular-num">
        第 {attemptNumber} 次投递
      </span>,
    )
  }
  if (record.backfilled) {
    chips.push(
      <span key="old" className="app-chip is-old">
        迁移前记录
      </span>,
    )
  }

  if (chips.length === 0) {
    return showFallbackDays ? (
      <span className="app-days-hint tabular-num">本阶段 {days} 天</span>
    ) : null
  }
  return <div className="app-chips">{chips}</div>
}
