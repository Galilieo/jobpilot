// 标题旁的当前岗位标签：查看、换成岗位库里的别的岗位、解除关联（PUT current_job_id）。
// 当前岗位只决定面板显示哪个岗位和工具的默认岗位，不限制 Agent 读别的岗位。
import { DownOutlined, SolutionOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { App, Button, Dropdown } from 'antd'
import type { MenuProps } from 'antd'
import type { ReactElement } from 'react'
import { conversations, jobs } from '../../api/endpoints'
import { JOBS_QUERY } from '../../queryKeys'

interface JobTagProps {
  conversationId: string
  currentJobId: string | null
  title: (jobId: string) => string
  onView: () => void
  // 绑定变了之后由页面刷新详情，并在对话里加一行提示。
  onChanged: (hint: string) => Promise<unknown>
}

export function JobTag(props: JobTagProps): ReactElement {
  const { message } = App.useApp()
  const list = useQuery({
    queryKey: JOBS_QUERY,
    queryFn: ({ signal }) => jobs.list(undefined, signal),
  })
  const bind = useMutation({
    mutationFn: (jobId: string | null) =>
      conversations.update(props.conversationId, { current_job_id: jobId }),
    onSuccess: (_, jobId) =>
      props.onChanged(jobId ? `当前岗位已切换为：${props.title(jobId)}` : '已解除当前岗位'),
    onError: (error) => void message.error(error.message),
  })

  const others = (list.data ?? []).filter((job) => job.job_id !== props.currentJobId)
  const switchItems: MenuProps['items'] =
    others.length > 0
      ? others.map((job) => ({ key: `job:${job.job_id}`, label: job.title ?? '未命名岗位' }))
      : [{ key: 'empty', label: '岗位库里没有别的岗位', disabled: true }]
  const items: MenuProps['items'] = [
    ...(props.currentJobId ? [{ key: 'view', label: '查看当前岗位' }] : []),
    {
      key: 'switch',
      label: props.currentJobId ? '换成别的岗位' : '选一个岗位',
      children: switchItems,
      popupClassName: 'job-switch-menu',
    },
    ...(props.currentJobId
      ? [{ type: 'divider' as const }, { key: 'unbind', label: '解除关联' }]
      : []),
  ]

  return (
    <Dropdown
      trigger={['click']}
      menu={{
        items,
        onClick: ({ key }) => {
          if (key === 'view') props.onView()
          else if (key === 'unbind') bind.mutate(null)
          else if (key.startsWith('job:')) bind.mutate(key.slice(4))
        },
      }}
    >
      <Button
        size="small"
        className={props.currentJobId ? 'job-tag' : 'job-tag is-empty'}
        loading={bind.isPending}
        icon={<SolutionOutlined />}
      >
        <span className="job-tag-label">
          {props.currentJobId ? props.title(props.currentJobId) : '未关联岗位'}
        </span>
        <DownOutlined />
      </Button>
    </Dropdown>
  )
}
