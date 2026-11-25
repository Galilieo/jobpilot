// 工作台（首页）：问候语、三张核心状态卡（事实库 / 不编造记录 / 偏好记忆）和四列岗位进度看板。
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Empty, Skeleton, Space, Tag, Typography, theme } from 'antd'
import type { ReactElement } from 'react'
import { useNavigate } from 'react-router'
import { overview } from '../api/endpoints'
import type { BoardJobCard, FactsOverview, GroundingOverview, JobBoard } from '../api/types'
import { useCurrentUser } from '../auth'
import { JOB_STAGE } from '../labels'
import { OVERVIEW_QUERY } from '../queryKeys'

function greetingPrefix(hour: number): string {
  if (hour < 12) return '早上好'
  if (hour < 18) return '下午好'
  return '晚上好'
}

export function WorkspacePage(): ReactElement {
  const navigate = useNavigate()
  const email = useCurrentUser().data?.email ?? ''
  const handle = email.split('@')[0] || '同学'
  const greeting = `${greetingPrefix(new Date().getHours())}，${handle}，今天准备哪个岗位？`
  const query = useQuery({
    queryKey: OVERVIEW_QUERY,
    queryFn: ({ signal }) => overview.get(signal),
  })

  return (
    <section className="page">
      <header className="page-heading page-heading-actions">
        <div>
          <Typography.Title level={1}>{greeting}</Typography.Title>
          <Typography.Text type="secondary">
            先确认经历事实，再匹配岗位并生成可追溯的定制简历。
          </Typography.Text>
        </div>
        <Space wrap>
          <Button onClick={() => void navigate('/me')}>查看经历事实库</Button>
          <Button type="primary" onClick={() => void navigate('/jobs')}>
            打开岗位库
          </Button>
        </Space>
      </header>

      {query.isPending ? (
        <Skeleton active />
      ) : query.isError ? (
        <Card>
          <Empty description={query.error.message}>
            <Button onClick={() => void query.refetch()}>重试</Button>
          </Empty>
        </Card>
      ) : (
        <Space orientation="vertical" size={24} className="full-width">
          <StatusCards facts={query.data.facts} grounding={query.data.grounding} />
          <BoardSection board={query.data.board} />
        </Space>
      )}
    </section>
  )
}

function StatusCards({
  facts,
  grounding,
}: {
  facts: FactsOverview
  grounding: GroundingOverview
}): ReactElement {
  const navigate = useNavigate()
  return (
    <div className="workspace-cards">
      <Card size="small" className="workspace-card">
        <Typography.Text type="secondary">经历事实库</Typography.Text>
        <div className="workspace-metric">
          <span>{facts.published_count}</span>
          <Typography.Text type="secondary" className="workspace-metric-sub">
            条 · {facts.version ? `v${facts.version}` : '尚未发布'}
          </Typography.Text>
        </div>
        <Typography.Paragraph type="secondary" className="workspace-card-note">
          {facts.pending_count > 0
            ? `${facts.pending_count} 条待确认，确认并发布后才会用于匹配和改写`
            : '当前没有待确认的条目'}
        </Typography.Paragraph>
        <Button size="small" onClick={() => void navigate('/me')}>
          {facts.pending_count > 0 ? '去确认' : '查看经历'}
        </Button>
      </Card>

      <Card size="small" className="workspace-card">
        <Typography.Text type="secondary">不编造记录</Typography.Text>
        <div className="workspace-metric">
          <span>{grounding.total}</span>
          <Typography.Text type="secondary" className="workspace-metric-sub">
            条改写 · {grounding.resume_count} 份定制简历
          </Typography.Text>
        </div>
        <Typography.Paragraph type="secondary" className="workspace-card-note">
          通过 {grounding.passed} 条 · 回退原文 {grounding.fallback} 条 · 待处理 {grounding.pending}{' '}
          条
        </Typography.Paragraph>
        <Button size="small" onClick={() => void navigate('/me?tab=resumes')}>
          查看定制简历
        </Button>
      </Card>

      <Card size="small" className="workspace-card">
        <Typography.Text type="secondary">记住的偏好</Typography.Text>
        <div className="workspace-metric">
          <Typography.Text type="secondary">之后开放</Typography.Text>
        </div>
        <Typography.Paragraph type="secondary" className="workspace-card-note">
          偏好记忆将在后续版本开放，只保存求职与写作偏好，不保存经历。
        </Typography.Paragraph>
      </Card>
    </div>
  )
}

const BOARD_COLUMNS: { key: keyof JobBoard; title: string }[] = [
  { key: 'unmatched', title: '未匹配' },
  { key: 'matched', title: '已匹配' },
  { key: 'tailored', title: '已生成简历' },
  { key: 'applied', title: '已投递' },
]

function BoardSection({ board }: { board: JobBoard }): ReactElement {
  const { token } = theme.useToken()
  const navigate = useNavigate()
  const total =
    board.unmatched.length + board.matched.length + board.tailored.length + board.applied.length

  return (
    <section aria-label="岗位进度">
      <div className="workspace-board-head">
        <Typography.Title level={4}>岗位进度</Typography.Title>
        <Typography.Text type="secondary">点击卡片进入岗位详情继续处理</Typography.Text>
      </div>
      {total === 0 ? (
        <Card>
          <Empty description="还没有添加岗位。去岗位库贴入一份 JD 开始。">
            <Button onClick={() => void navigate('/jobs')}>打开岗位库</Button>
          </Empty>
        </Card>
      ) : (
        <div className="workspace-board">
          {BOARD_COLUMNS.map(({ key, title }) => {
            const items = board[key]
            return (
              <div
                key={key}
                className="board-column"
                style={{ background: token.colorFillQuaternary }}
              >
                <div className="board-column-head">
                  <Typography.Text strong>{title}</Typography.Text>
                  <Typography.Text type="secondary" className="tabular-num">
                    {items.length}
                  </Typography.Text>
                </div>
                {items.length === 0 ? (
                  <Typography.Text type="secondary" className="board-column-empty">
                    暂无
                  </Typography.Text>
                ) : (
                  items.map((card) => (
                    <BoardCard
                      key={card.job_id}
                      card={card}
                      showStage={key === 'applied'}
                      onOpen={() => void navigate(`/jobs/${card.job_id}`)}
                    />
                  ))
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

function BoardCard({
  card,
  showStage,
  onOpen,
}: {
  card: BoardJobCard
  showStage: boolean
  onOpen: () => void
}): ReactElement {
  const subtitle = [card.company, card.location].filter(Boolean).join(' · ') || '公司未填写'
  return (
    <Card size="small" hoverable className="board-card" onClick={onOpen}>
      <Typography.Text strong>{card.title ?? '未命名岗位'}</Typography.Text>
      <Typography.Paragraph type="secondary" className="board-card-sub">
        {subtitle}
      </Typography.Paragraph>
      <Space size={4} wrap>
        {showStage && <Tag color="blue">{JOB_STAGE[card.stage]}</Tag>}
        {card.resume_stale && <Tag color="warning">事实库已更新</Tag>}
      </Space>
    </Card>
  )
}
