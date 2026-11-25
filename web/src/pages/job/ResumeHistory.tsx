// 定制简历面板的历史页签：版本历史（只读预览与导出旧版）与同岗位改写运行历史。
import { ReloadOutlined } from '@ant-design/icons'
import { Button, Dropdown, Empty, Space, Tag, Typography } from 'antd'
import type { ReactElement } from 'react'
import { tailoring } from '../../api/endpoints'
import type { TailorRunSummary, VersionSummary } from '../../api/types'
import { RUN_STATUS, STOP_REASON, formatDateTime } from '../../labels'

interface ResumeHistoryProps {
  runId: string | undefined
  versions: VersionSummary[] | undefined
  currentVersionId: string | undefined
  selectedVersion: number | undefined
  runs: TailorRunSummary[]
  onPreviewVersion: (version: number | undefined) => void
  onRefreshVersions: () => void
  onSelectRun: (runId: string) => void
}

export function ResumeHistory({
  runId,
  versions,
  currentVersionId,
  selectedVersion,
  runs,
  onPreviewVersion,
  onRefreshVersions,
  onSelectRun,
}: ResumeHistoryProps): ReactElement {
  return (
    <Space orientation="vertical" size={20} className="full-width">
      {runId && versions && (
        <section>
          <div className="fact-group-head">
            <Typography.Title level={5}>版本历史</Typography.Title>
            <Button size="small" icon={<ReloadOutlined />} onClick={onRefreshVersions}>
              刷新版本
            </Button>
          </div>
          <ul className="row-list">
            {[...versions].reverse().map((item) => {
              const isCurrent = item.version_id === currentVersionId
              const isPreviewing =
                selectedVersion === item.version || (selectedVersion === undefined && isCurrent)
              return (
                <li key={item.version_id} className="fact-item">
                  <div className="fact-row">
                    <Space size={6} wrap>
                      <Typography.Text strong className="tabular-num">
                        第 {item.version} 版
                      </Typography.Text>
                      {isCurrent && <Tag color="blue">当前版本</Tag>}
                      {isPreviewing && !isCurrent && <Tag color="processing">正在只读预览</Tag>}
                    </Space>
                    <Typography.Text type="secondary" className="tabular-num">
                      {formatDateTime(item.created_at)}
                    </Typography.Text>
                  </div>
                  <div className="fact-actions">
                    {isCurrent && selectedVersion !== undefined ? (
                      <Button size="small" onClick={() => onPreviewVersion(undefined)}>
                        回到当前版本
                      </Button>
                    ) : !isCurrent ? (
                      <Button
                        size="small"
                        disabled={selectedVersion === item.version}
                        onClick={() => onPreviewVersion(item.version)}
                      >
                        预览旧版
                      </Button>
                    ) : null}
                    <Dropdown
                      menu={{
                        items: [
                          {
                            key: 'html',
                            label: (
                              <a href={tailoring.exportUrl(runId, 'html', item.version)}>
                                HTML（可继续编辑）
                              </a>
                            ),
                          },
                          {
                            key: 'docx',
                            label: (
                              <a href={tailoring.exportUrl(runId, 'docx', item.version)}>
                                Word（DOCX）
                              </a>
                            ),
                          },
                          {
                            key: 'md',
                            label: (
                              <a href={tailoring.exportUrl(runId, 'md', item.version)}>Markdown</a>
                            ),
                          },
                        ],
                      }}
                    >
                      <Button size="small">导出</Button>
                    </Dropdown>
                  </div>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      <section>
        <Typography.Title level={5}>运行历史</Typography.Title>
        {runs.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有改写运行记录" />
        ) : (
          <ul className="row-list">
            {runs.map((item) => {
              const isSelected = item.run_id === runId
              const statusColor =
                item.status === 'succeeded'
                  ? 'success'
                  : item.status === 'running'
                    ? 'processing'
                    : item.status === 'budget_exhausted'
                      ? 'warning'
                      : 'error'
              return (
                <li key={item.run_id} className="fact-item">
                  <div className="fact-row">
                    <Space size={6} wrap>
                      <Tag color={statusColor}>{RUN_STATUS[item.status]}</Tag>
                      <Typography.Text type="secondary" className="tabular-num">
                        {formatDateTime(item.started_at)}
                      </Typography.Text>
                    </Space>
                    {item.stop_reason && (
                      <Typography.Text type="secondary">
                        {STOP_REASON[item.stop_reason] ?? item.stop_reason}
                      </Typography.Text>
                    )}
                    {!item.stop_reason && item.status === 'failed' && (
                      <Typography.Text type="secondary">这次运行没有成功完成</Typography.Text>
                    )}
                  </div>
                  <Button
                    size="small"
                    disabled={isSelected}
                    onClick={() => onSelectRun(item.run_id)}
                  >
                    {isSelected ? '当前查看' : '查看'}
                  </Button>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </Space>
  )
}
