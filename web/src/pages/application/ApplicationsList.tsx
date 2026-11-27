// 投递页列表视图：表格展示投递记录，支持按投递日期或最近变动排序，阶段下拉带日期确认弹层。
import { Card, Select, Table, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactElement } from 'react'
import type { ApplicationListItem, ApplicationStage } from '../../api/types'
import { APPLICATION_STAGE } from '../../labels'
import { ApplicationChips } from './ApplicationChips'
import { StageChangePopover } from './StageChangePopover'
import {
  BOARD_STAGES,
  sortApplicationsForList,
  type ApplicationSortField,
} from './applicationStatus'

interface ApplicationsListProps {
  items: readonly ApplicationListItem[]
  showRejected: boolean
  sortField: ApplicationSortField
  onSortChange: (field: ApplicationSortField) => void
  attemptNumbers: Map<string, number>
  today: string
  onSelect: (applicationId: string) => void
  refresh: () => Promise<void>
}

const STAGE_OPTIONS = BOARD_STAGES.map((stage) => ({
  value: stage,
  label: APPLICATION_STAGE[stage],
}))

function StageSelectCell({
  record,
  refresh,
}: {
  record: ApplicationListItem
  refresh: () => Promise<void>
}): ReactElement {
  const [pendingStage, setPendingStage] = useState<ApplicationStage | null>(null)

  return (
    <div
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <StageChangePopover
        record={record}
        refresh={refresh}
        fixedStage={pendingStage ?? record.stage}
        open={pendingStage !== null}
        onOpenChange={(next) => {
          if (!next) setPendingStage(null)
        }}
        onCancel={() => setPendingStage(null)}
      >
        <Select<ApplicationStage>
          size="small"
          aria-label={`修改「${record.job_snapshot_json.title ?? '未命名岗位'}」阶段`}
          value={pendingStage ?? record.stage}
          options={STAGE_OPTIONS}
          onChange={(nextStage) => {
            if (nextStage !== record.stage) setPendingStage(nextStage)
          }}
        />
      </StageChangePopover>
    </div>
  )
}

export function ApplicationsList({
  items,
  showRejected,
  sortField,
  onSortChange,
  attemptNumbers,
  today,
  onSelect,
  refresh,
}: ApplicationsListProps): ReactElement {
  const visibleRows = sortApplicationsForList(
    items.filter((item) => showRejected || item.stage !== 'rejected'),
    sortField,
  )

  const columns: ColumnsType<ApplicationListItem> = [
    {
      title: '岗位',
      key: 'job',
      render: (_, record) => {
        const companyParts = [
          record.job_snapshot_json.company,
          record.job_snapshot_json.location,
        ].filter((part): part is string => Boolean(part))
        return (
          <div>
            <Typography.Text strong>
              {record.job_snapshot_json.title ?? '未命名岗位'}
            </Typography.Text>
            {companyParts.length > 0 && (
              <div className="app-list-company">{companyParts.join(' · ')}</div>
            )}
          </div>
        )
      },
    },
    {
      title: '阶段',
      key: 'stage',
      width: 132,
      render: (_, record) => <StageSelectCell record={record} refresh={refresh} />,
    },
    {
      title: (
        <button
          type="button"
          className="app-sort-header"
          onClick={() => onSortChange('applied_at')}
        >
          投递日期{sortField === 'applied_at' ? ' ↓' : ''}
        </button>
      ),
      dataIndex: 'applied_at',
      key: 'applied_at',
      className: 'tabular-num',
      width: 120,
    },
    {
      title: '渠道',
      key: 'channel',
      width: 110,
      render: (_, record) => record.channel ?? '—',
    },
    {
      title: '简历版本',
      key: 'resume_version',
      className: 'tabular-num',
      width: 100,
      render: (_, record) =>
        record.resume_version != null ? `第 ${record.resume_version} 版` : '—',
    },
    {
      title: (
        <button
          type="button"
          className="app-sort-header"
          onClick={() => onSortChange('stage_changed_at')}
        >
          最近变动{sortField === 'stage_changed_at' ? ' ↓' : ''}
        </button>
      ),
      dataIndex: 'stage_changed_at',
      key: 'stage_changed_at',
      className: 'tabular-num',
      width: 120,
    },
    {
      title: '标记',
      key: 'chips',
      render: (_, record) => (
        <ApplicationChips
          record={record}
          attemptNumber={attemptNumbers.get(record.application_id)}
          today={today}
        />
      ),
    },
  ]

  return (
    <Card className="table-card">
      <Table<ApplicationListItem>
        rowKey="application_id"
        columns={columns}
        dataSource={visibleRows}
        pagination={false}
        scroll={{ x: 'max-content' }}
        onRow={(record) => ({
          className: 'app-table-row',
          onClick: () => onSelect(record.application_id),
        })}
      />
    </Card>
  )
}
