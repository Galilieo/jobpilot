// 投递页看板视图：按投递阶段分列，列内按进入当前阶段日期倒序排列。
import type { CSSProperties, KeyboardEvent, ReactElement } from 'react'
import type { ApplicationListItem, ApplicationStage } from '../../api/types'
import { MetaList } from '../../components/MetaList'
import { APPLICATION_STAGE } from '../../labels'
import { ApplicationChips } from './ApplicationChips'
import { StageChangePopover } from './StageChangePopover'
import { BOARD_STAGES, sortByStageChangedDesc } from './applicationStatus'

interface ApplicationsBoardProps {
  items: readonly ApplicationListItem[]
  showRejected: boolean
  attemptNumbers: Map<string, number>
  today: string
  onSelect: (applicationId: string) => void
  refresh: () => Promise<void>
}

export function ApplicationsBoard({
  items,
  showRejected,
  attemptNumbers,
  today,
  onSelect,
  refresh,
}: ApplicationsBoardProps): ReactElement {
  const columns = BOARD_STAGES.filter((stage) => showRejected || stage !== 'rejected')

  return (
    <div
      className="applications-board"
      style={{ '--board-cols': String(columns.length) } as CSSProperties}
    >
      {columns.map((stage) => {
        const columnItems = sortByStageChangedDesc(items.filter((item) => item.stage === stage))
        return (
          <BoardColumn
            key={stage}
            stage={stage}
            items={columnItems}
            attemptNumbers={attemptNumbers}
            today={today}
            onSelect={onSelect}
            refresh={refresh}
          />
        )
      })}
    </div>
  )
}

function BoardColumn({
  stage,
  items,
  attemptNumbers,
  today,
  onSelect,
  refresh,
}: {
  stage: ApplicationStage
  items: ApplicationListItem[]
  attemptNumbers: Map<string, number>
  today: string
  onSelect: (applicationId: string) => void
  refresh: () => Promise<void>
}): ReactElement {
  return (
    <section
      className={stage === 'rejected' ? 'app-column is-rejected' : 'app-column'}
      aria-label={`${APPLICATION_STAGE[stage]}列`}
    >
      <div className="app-column-head">
        <span className={`app-stage-dot is-${stage}`} aria-hidden="true" />
        <span className="app-column-title">{APPLICATION_STAGE[stage]}</span>
        <span className="app-column-count tabular-num">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="app-column-empty">
          {stage === 'applied' ? '在岗位详情点「记录投递」，或在对话里说「我投了」。' : '暂时没有'}
        </div>
      ) : (
        <div className="app-column-cards">
          {items.map((record) => (
            <ApplicationCard
              key={record.application_id}
              record={record}
              attemptNumber={attemptNumbers.get(record.application_id)}
              today={today}
              onSelect={onSelect}
              refresh={refresh}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function ApplicationCard({
  record,
  attemptNumber,
  today,
  onSelect,
  refresh,
}: {
  record: ApplicationListItem
  attemptNumber?: number
  today: string
  onSelect: (applicationId: string) => void
  refresh: () => Promise<void>
}): ReactElement {
  const companyParts = [record.job_snapshot_json.company, record.job_snapshot_json.location].filter(
    (part): part is string => Boolean(part),
  )
  const metaItems = [
    `投递 ${record.applied_at.slice(5)}`,
    record.channel,
    record.resume_version != null ? `第 ${record.resume_version} 版简历` : null,
  ].filter((part): part is string => Boolean(part))

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect(record.application_id)
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
      className="app-card"
      onClick={() => onSelect(record.application_id)}
      onKeyDown={handleKeyDown}
    >
      <div className="app-card-title">{record.job_snapshot_json.title ?? '未命名岗位'}</div>
      {companyParts.length > 0 && (
        <div className="app-card-company">{companyParts.join(' · ')}</div>
      )}
      <MetaList className="app-card-meta tabular-num" items={metaItems} />
      <div className="app-card-foot">
        <ApplicationChips
          record={record}
          attemptNumber={attemptNumber}
          today={today}
          showFallbackDays
        />
        <span className="app-card-spacer" />
        <StageChangePopover record={record} refresh={refresh} />
      </div>
    </div>
  )
}
