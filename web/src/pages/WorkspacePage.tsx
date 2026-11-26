// 工作台（首页）：问候语、三张核心状态卡（事实库 / 不编造记录 / 偏好记忆）和四列等高岗位进度看板。
import { BookOutlined, BulbOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Skeleton, Space, Tag, Typography, theme } from 'antd'
import type { ReactElement } from 'react'
import { useNavigate } from 'react-router'
import { overview } from '../api/endpoints'
import type { BoardJobCard, FactsOverview, GroundingOverview, JobBoard } from '../api/types'
import { useCurrentUser } from '../auth'
import { EmptyHint } from '../components/EmptyHint'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { JOB_STAGE, formatJobMeta, formatMatchSummary } from '../labels'
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
    <section className="page workspace-page">
      <PageHeader
        title={greeting}
        subtitle="先确认经历事实，再匹配岗位并生成可追溯的定制简历。"
        extra={
          <Button type="primary" onClick={() => void navigate('/jobs')}>
            打开岗位库
          </Button>
        }
      />

      {query.isPending ? (
        <Skeleton active />
      ) : query.isError ? (
        <Card>
          <EmptyHint
            description={query.error.message}
            action={<Button onClick={() => void query.refetch()}>重试</Button>}
          />
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
    <div className="stat-cards">
      <StatCard
        label={<StatLabel icon={<BookOutlined />} tone="primary" text="经历事实库" />}
        value={facts.published_count}
        unit={`条 · ${facts.version ? `v${facts.version}` : '尚未发布'}`}
        note={
          facts.pending_count > 0
            ? `${facts.pending_count} 条待确认，确认并发布后才会用于匹配和改写`
            : '当前没有待确认的条目'
        }
        action={
          <Button size="small" onClick={() => void navigate('/me')}>
            {facts.pending_count > 0 ? '去确认' : '查看经历'}
          </Button>
        }
      />
      <StatCard
        label={<StatLabel icon={<SafetyCertificateOutlined />} tone="success" text="不编造记录" />}
        value={grounding.total}
        unit={`条改写 · ${grounding.resume_count} 份定制简历`}
        note={`通过 ${grounding.passed} 条 · 回退原文 ${grounding.fallback} 条 · 待处理 ${grounding.pending} 条`}
        action={
          <Button size="small" onClick={() => void navigate('/me?tab=resumes')}>
            查看定制简历
          </Button>
        }
      />
      <StatCard
        label={<StatLabel icon={<BulbOutlined />} tone="muted" text="记住的偏好" />}
        unit="之后开放"
        note="偏好记忆将在后续版本开放，只保存求职与写作偏好，不保存经历。"
      />
    </div>
  )
}

function StatLabel({
  icon,
  tone,
  text,
}: {
  icon: ReactElement
  tone: 'primary' | 'success' | 'muted'
  text: string
}): ReactElement {
  const { token } = theme.useToken()
  const tones = {
    primary: { background: token.colorPrimaryBg, color: token.colorPrimaryText },
    success: { background: token.colorSuccessBg, color: token.colorSuccessText },
    muted: { background: token.colorFillTertiary, color: token.colorTextSecondary },
  }
  return (
    <span className="stat-card-label-row">
      <span className="stat-card-icon" style={tones[tone]}>
        {icon}
      </span>
      {text}
    </span>
  )
}

const BOARD_COLUMNS: {
  key: keyof JobBoard
  title: string
  dot: 'muted' | 'primary' | 'success' | 'warning'
}[] = [
  { key: 'unmatched', title: '未匹配', dot: 'muted' },
  { key: 'matched', title: '已匹配', dot: 'primary' },
  { key: 'tailored', title: '已生成简历', dot: 'success' },
  { key: 'applied', title: '已投递', dot: 'warning' },
]

function BoardSection({ board }: { board: JobBoard }): ReactElement {
  const { token } = theme.useToken()
  const navigate = useNavigate()
  const dotColors = {
    muted: token.colorTextTertiary,
    primary: token.colorPrimary,
    success: token.colorSuccess,
    warning: token.colorWarning,
  }
  const total =
    board.unmatched.length + board.matched.length + board.tailored.length + board.applied.length

  return (
    <section aria-label="岗位进度">
      <div className="workspace-board-head">
        <Typography.Title level={3}>岗位进度</Typography.Title>
        <Typography.Text type="secondary" className="board-head-sub">
          点击卡片进入岗位详情继续处理
        </Typography.Text>
      </div>
      {total === 0 ? (
        <Card>
          <EmptyHint
            description="还没有添加岗位。去岗位库贴入一份 JD 开始。"
            action={<Button onClick={() => void navigate('/jobs')}>打开岗位库</Button>}
          />
        </Card>
      ) : (
        <div className="workspace-board">
          {BOARD_COLUMNS.map(({ key, title, dot }) => {
            const items = board[key]
            return (
              <div key={key} className="board-column">
                <div className="board-column-head">
                  <span className="board-column-title">
                    <span className="board-dot" style={{ background: dotColors[dot] }} />
                    <Typography.Text strong>{title}</Typography.Text>
                  </span>
                  <Typography.Text type="secondary" className="board-count tabular-num">
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
                      columnKey={key}
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
  columnKey,
  onOpen,
}: {
  card: BoardJobCard
  columnKey: keyof JobBoard
  onOpen: () => void
}): ReactElement {
  const { token } = theme.useToken()
  const subtitle = formatJobMeta(card)
  return (
    <Card size="small" hoverable className="board-card" onClick={onOpen}>
      <Typography.Text strong>{card.title ?? '未命名岗位'}</Typography.Text>
      {card.salary && (
        <Typography.Text strong className="board-card-salary" style={{ color: token.colorPrimary }}>
          {card.salary}
        </Typography.Text>
      )}
      <Typography.Paragraph type="secondary" className="board-card-sub">
        {subtitle}
      </Typography.Paragraph>
      <div className="board-card-footer">
        <Typography.Text type="secondary" className="tabular-num">
          {formatMatchSummary(card.match_summary)}
        </Typography.Text>
        {card.resume_stale && <Tag color="warning">事实库已更新</Tag>}
        {columnKey === 'applied' && <Tag color="processing">{JOB_STAGE[card.stage]}</Tag>}
      </div>
    </Card>
  )
}
