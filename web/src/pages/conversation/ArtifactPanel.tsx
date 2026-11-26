// 右侧产物面板：当前岗位的定制简历、匹配报告和岗位信息。
// 三个标签直接复用岗位详情页的组件，编辑和保存逻辑不另写一份。
import { CloseOutlined, CompressOutlined, ExpandOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Button, Skeleton, Tabs, Tooltip } from 'antd'
import type { ReactElement } from 'react'
import { jobs, tailoring } from '../../api/endpoints'
import { JobInfo } from '../job/JobInfo'
import { MatchPanel } from '../job/MatchPanel'
import { ResumePanel } from '../job/ResumePanel'
import { TAILORED_RESUMES_QUERY, jobQuery, matchesQuery } from '../../queryKeys'
import { PANEL_TAB_LABEL, panelTabs, type PanelTab } from './panelTabs'

interface ArtifactPanelProps {
  jobId: string
  // 对话里确认或撤销了写入时变化。简历预览的 iframe 自己保存、自己持有版本号，
  // 外面改了简历要整个重建，否则它会拿旧版本号保存而冲突。
  writeKey: string
  activeTab: PanelTab | null
  onTabChange: (tab: PanelTab) => void
  expanded?: boolean
  onToggleExpand?: () => void
  onClose?: () => void
}

function JobTab({ jobId }: { jobId: string }): ReactElement {
  const job = useQuery({
    queryKey: jobQuery(jobId),
    queryFn: ({ signal }) => jobs.get(jobId, signal),
  })
  return job.data ? <JobInfo job={job.data} /> : <Skeleton active />
}

function tabContent(tab: PanelTab, jobId: string, writeKey: string): ReactElement {
  if (tab === 'resume') return <ResumePanel key={writeKey} jobId={jobId} />
  if (tab === 'match') return <MatchPanel jobId={jobId} embedded />
  return <JobTab jobId={jobId} />
}

export function ArtifactPanel(props: ArtifactPanelProps): ReactElement {
  const { jobId } = props
  const matches = useQuery({
    queryKey: matchesQuery(jobId),
    queryFn: ({ signal }) => jobs.matches(jobId, signal),
  })
  const resumes = useQuery({
    queryKey: TAILORED_RESUMES_QUERY,
    queryFn: ({ signal }) => tailoring.listAll(signal),
  })

  const tabs = panelTabs({
    hasMatch: (matches.data?.length ?? 0) > 0,
    hasResume: (resumes.data ?? []).some((item) => item.job_id === jobId),
  })
  const active = props.activeTab && tabs.includes(props.activeTab) ? props.activeTab : tabs[0]

  const actions = (
    <span className="artifact-panel-actions">
      {props.onToggleExpand && (
        <Tooltip title={props.expanded ? '还原' : '放大'}>
          <Button
            type="text"
            size="small"
            aria-label={props.expanded ? '还原面板' : '放大面板'}
            icon={props.expanded ? <CompressOutlined /> : <ExpandOutlined />}
            onClick={props.onToggleExpand}
          />
        </Tooltip>
      )}
      {props.onClose && (
        <Tooltip title="关闭面板">
          <Button
            type="text"
            size="small"
            aria-label="关闭面板"
            icon={<CloseOutlined />}
            onClick={props.onClose}
          />
        </Tooltip>
      )}
    </span>
  )

  return (
    <Tabs
      className="artifact-panel-tabs"
      activeKey={active}
      onChange={(key) => props.onTabChange(key as PanelTab)}
      tabBarExtraContent={actions}
      destroyOnHidden
      items={tabs.map((tab) => ({
        key: tab,
        label: PANEL_TAB_LABEL[tab],
        children: (
          <div className="artifact-panel-body">{tabContent(tab, jobId, props.writeKey)}</div>
        ),
      }))}
    />
  )
}
