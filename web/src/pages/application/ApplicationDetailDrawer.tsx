// 投递详情右侧抽屉：展示投递时的岗位快照、当前阶段，并复用投递行能力。
import { Alert, Drawer, Grid, Typography } from 'antd'
import type { ReactElement } from 'react'
import { Link } from 'react-router'
import type { ApplicationListItem } from '../../api/types'
import { MetaList } from '../../components/MetaList'
import { APPLICATION_STAGE } from '../../labels'
import { ApplicationChips } from './ApplicationChips'
import { ApplicationRow } from './ApplicationRecords'
import { StageChangePopover } from './StageChangePopover'

interface ApplicationDetailDrawerProps {
  record: ApplicationListItem | null
  attemptNumber?: number
  open: boolean
  onClose: () => void
  refresh: () => Promise<void>
}

export function ApplicationDetailDrawer({
  record,
  attemptNumber,
  open,
  onClose,
  refresh,
}: ApplicationDetailDrawerProps): ReactElement {
  const screens = Grid.useBreakpoint()
  const isNarrow = screens.lg === false

  const companyMeta = record
    ? [record.job_snapshot_json.company, record.job_snapshot_json.location].filter(
        (field): field is string => Boolean(field),
      )
    : []

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      size={isNarrow ? '100%' : 520}
      destroyOnHidden
      title={
        record ? (
          <div className="app-drawer-title">
            <div>{record.job_snapshot_json.title ?? '未命名岗位'}</div>
            {(companyMeta.length > 0 || record.user_job_id) && (
              <div className="app-drawer-head-sub">
                {companyMeta.length > 0 && <MetaList items={companyMeta} />}
                {record.user_job_id && (
                  <Link to={`/jobs/${record.user_job_id}`} onClick={onClose}>
                    打开岗位详情
                  </Link>
                )}
              </div>
            )}
          </div>
        ) : (
          '投递详情'
        )
      }
    >
      {record && (
        <div className="app-drawer-body">
          <ApplicationChips record={record} attemptNumber={attemptNumber} />
          {record.user_job_id === null && (
            <Alert
              type="info"
              showIcon
              title="原岗位已从岗位库删除，这里显示投递时记下的岗位信息和简历快照。"
            />
          )}
          <div className="app-drawer-stage-bar">
            <div>
              <Typography.Text strong>{APPLICATION_STAGE[record.stage]}</Typography.Text>
              <Typography.Text type="secondary" className="app-drawer-stage-since tabular-num">
                自 {record.stage_changed_at} 起
              </Typography.Text>
            </div>
            <StageChangePopover record={record} refresh={refresh} triggerLabel="改阶段" />
          </div>
          <ApplicationRow
            record={record}
            refresh={refresh}
            showJobInfo={false}
            defaultHistoryOpen
            onDeleted={onClose}
          />
        </div>
      )}
    </Drawer>
  )
}
