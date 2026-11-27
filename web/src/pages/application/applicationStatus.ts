// 投递阶段、停留天数、没进展判断、同岗位第几次投递编号与排序规则。
import type { ApplicationListItem, ApplicationRecord, ApplicationStage } from '../../api/types'

export const STALE_AFTER_DAYS = 14

export const ACTIVE_STAGES: readonly ApplicationStage[] = [
  'applied',
  'written_test',
  'interviewing',
]

export const BOARD_STAGES: readonly ApplicationStage[] = [
  'applied',
  'written_test',
  'interviewing',
  'offer',
  'rejected',
]

export type ApplicationSortField = 'stage_changed_at' | 'applied_at'

const MS_PER_DAY = 86_400_000

export function todayLocalDate(now = new Date()): string {
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function parseUtcDayMs(dateText: string): number {
  const [year, month, day] = dateText.split('-').map(Number)
  return Date.UTC(year, (month || 1) - 1, day || 1)
}

export function daysBetweenDates(fromDate: string, toDate: string): number {
  return Math.round((parseUtcDayMs(toDate) - parseUtcDayMs(fromDate)) / MS_PER_DAY)
}

export function daysInCurrentStage(stageChangedAt: string, today = todayLocalDate()): number {
  return Math.max(0, daysBetweenDates(stageChangedAt, today))
}

export function isStaleApplication(
  item: Pick<ApplicationListItem, 'stage' | 'stage_changed_at'>,
  today = todayLocalDate(),
): boolean {
  return (
    ACTIVE_STAGES.includes(item.stage) &&
    daysInCurrentStage(item.stage_changed_at, today) > STALE_AFTER_DAYS
  )
}

export function applicationAttemptNumbers(
  items: readonly ApplicationRecord[],
): Map<string, number> {
  const byJob = new Map<string, ApplicationRecord[]>()
  for (const item of items) {
    if (!item.user_job_id) continue
    const group = byJob.get(item.user_job_id) ?? []
    group.push(item)
    byJob.set(item.user_job_id, group)
  }
  const result = new Map<string, number>()
  for (const group of byJob.values()) {
    if (group.length <= 1) continue
    const ordered = [...group].sort(
      (a, b) =>
        a.applied_at.localeCompare(b.applied_at) ||
        a.created_at.localeCompare(b.created_at) ||
        a.application_id.localeCompare(b.application_id),
    )
    ordered.forEach((item, index) => result.set(item.application_id, index + 1))
  }
  return result
}

type SortableApplication = Pick<
  ApplicationListItem,
  'stage_changed_at' | 'applied_at' | 'created_at' | 'application_id'
>

function compareDesc(
  a: SortableApplication,
  b: SortableApplication,
  primary: ApplicationSortField,
) {
  return (
    b[primary].localeCompare(a[primary]) ||
    b.stage_changed_at.localeCompare(a.stage_changed_at) ||
    b.applied_at.localeCompare(a.applied_at) ||
    b.created_at.localeCompare(a.created_at) ||
    b.application_id.localeCompare(a.application_id)
  )
}

export function sortByStageChangedDesc<T extends SortableApplication>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => compareDesc(a, b, 'stage_changed_at'))
}

export function sortApplicationsForList<T extends SortableApplication>(
  items: readonly T[],
  field: ApplicationSortField,
): T[] {
  return [...items].sort((a, b) => compareDesc(a, b, field))
}

export function summarizeApplications(
  items: readonly ApplicationListItem[],
  today = todayLocalDate(),
): { total: number; waiting: number; stale: number; offer: number } {
  let waiting = 0
  let stale = 0
  let offer = 0
  for (const item of items) {
    if (ACTIVE_STAGES.includes(item.stage)) {
      waiting += 1
      if (isStaleApplication(item, today)) stale += 1
    } else if (item.stage === 'offer') {
      offer += 1
    }
  }
  return { total: items.length, waiting, stale, offer }
}
