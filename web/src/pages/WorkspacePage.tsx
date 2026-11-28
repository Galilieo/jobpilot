// 工作台（首页）：宋体问候语、顶部发消息新建对话、横向三栏统计（含记住的偏好）和四列看板。
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Skeleton, Space, Tag, Typography, theme } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { Link, useNavigate } from 'react-router'
import { memories, overview } from '../api/endpoints'
import type {
  BoardJobCard,
  FactsOverview,
  GroundingOverview,
  JobBoard,
  MemoryCollection,
} from '../api/types'
import { useCurrentUser } from '../auth'
import { EmptyHint } from '../components/EmptyHint'
import { MetaList } from '../components/MetaList'
import { JOB_STAGE, formatMatchSummary, jobMetaParts } from '../labels'
import { MEMORIES_QUERY, OVERVIEW_QUERY } from '../queryKeys'
import { Composer, type Draft } from './conversation/Composer'
import { useStartConversation } from './conversation/useStartConversation'

const EMPTY_DRAFT: Draft = { text: '', attachments: [] }

function greetingPrefix(hour: number): string {
  if (hour < 12) return '早上好'
  if (hour < 18) return '下午好'
  return '晚上好'
}

export function WorkspacePage(): ReactElement {
  const email = useCurrentUser().data?.email ?? ''
  const handle = email.split('@')[0] || '同学'
  const greeting = `${greetingPrefix(new Date().getHours())}，${handle}`
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const composerRef = useRef<HTMLDivElement>(null)
  const { start, creating } = useStartConversation()

  const query = useQuery({
    queryKey: OVERVIEW_QUERY,
    queryFn: ({ signal }) => overview.get(signal),
  })
  const memoriesQuery = useQuery({
    queryKey: MEMORIES_QUERY,
    queryFn: ({ signal }) => memories.list(signal),
  })

  function submit(): void {
    void start({
      clientMessageId: crypto.randomUUID(),
      content: draft.text.trim(),
      attachments: draft.attachments,
    })
  }

  return (
    <section className="page workspace-page">
      <header className="workspace-head">
        <h1 className="workspace-greeting serif-heading">{greeting}</h1>
        <Typography.Paragraph type="secondary" className="workspace-subtitle">
          今天想准备哪个岗位？发出后会新开一段对话。
        </Typography.Paragraph>
      </header>

      <div className="workspace-composer-wrap">
        <Composer
          variant="prominent"
          minRows={2}
          hideNote
          containerRef={composerRef}
          placeholder="粘贴一段 JD，或者说说你想做什么……"
          draft={draft}
          onChange={setDraft}
          onSubmit={submit}
          blockedReason={creating ? '正在新建对话…' : null}
        />
      </div>

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
        <Space orientation="vertical" size={28} className="full-width">
          <LedgerBar
            facts={query.data.facts}
            grounding={query.data.grounding}
            memoriesList={memoriesQuery.data}
          />
          <BoardSection
            board={query.data.board}
            onFocusComposer={() => composerRef.current?.querySelector('textarea')?.focus()}
          />
        </Space>
      )}
    </section>
  )
}

function LedgerBar({
  facts,
  grounding,
  memoriesList,
}: {
  facts: FactsOverview
  grounding: GroundingOverview
  memoriesList: MemoryCollection | undefined
}): ReactElement {
  const activeMemories = memoriesList?.entries ?? []
  const memoryPreview = activeMemories
    .slice(0, 2)
    .map((item) => item.content)
    .join(' · ')

  return (
    <div className="workspace-ledger">
      <div className="ledger-cell">
        <small>经历事实</small>
        <div className="ledger-metric">
          <strong className="serif-heading tabular-num">{facts.published_count}</strong>
          <span>条，{facts.version ? `版本 v${facts.version}` : '尚未发布'}</span>
        </div>
        <p className="ledger-note">
          <span>
            {facts.pending_count > 0 ? `${facts.pending_count} 条待确认` : '没有待确认的条目'}
          </span>
          <Link to="/me">{facts.pending_count > 0 ? '去确认' : '查看经历'}</Link>
        </p>
      </div>

      <div className="ledger-cell">
        <small>改写记录</small>
        <div className="ledger-metric">
          <strong className="serif-heading tabular-num">{grounding.total}</strong>
          <span>
            条，
            {grounding.fallback === 0 && grounding.pending === 0
              ? '全部可追溯'
              : `可追溯 ${grounding.passed} 条`}
          </span>
        </div>
        <p className="ledger-note">
          <span className="tabular-num">{grounding.fallback} 条回退到原文</span>
          <Link to="/me?tab=resumes">查看定制简历</Link>
        </p>
      </div>

      <div className="ledger-cell">
        <small>记住的偏好</small>
        <div className="ledger-metric">
          <strong className="serif-heading tabular-num">
            {memoriesList ? activeMemories.length : '—'}
          </strong>
          <span>项</span>
        </div>
        <p className="ledger-note">
          <span className="ledger-memory-preview">{memoryPreview || '还没有保存的偏好'}</span>
          <Link to="/me?tab=memories">管理</Link>
        </p>
      </div>
    </div>
  )
}

const BOARD_COLUMNS: {
  key: keyof JobBoard
  title: string
  dot: 'muted' | 'primary' | 'success' | 'warning'
  emptyText: string
}[] = [
  { key: 'unmatched', title: '未匹配', dot: 'muted', emptyText: '发一段 JD 就会出现在这里。' },
  {
    key: 'matched',
    title: '已匹配',
    dot: 'primary',
    emptyText: '完成匹配分析后会出现在这里。',
  },
  {
    key: 'tailored',
    title: '已生成简历',
    dot: 'success',
    emptyText: '生成定制简历后会出现在这里。',
  },
  {
    key: 'applied',
    title: '已投递',
    dot: 'warning',
    emptyText: '记录投递后会出现在这里。',
  },
]

function BoardSection({
  board,
  onFocusComposer,
}: {
  board: JobBoard
  onFocusComposer: () => void
}): ReactElement {
  const { token } = theme.useToken()
  const navigate = useNavigate()
  const dotColors = {
    muted: token.colorTextTertiary,
    primary: token.colorPrimary,
    success: token.colorSuccess,
    warning: token.colorWarning,
  }

  return (
    <section aria-label="岗位进度">
      <div className="workspace-board-head">
        <Typography.Title level={3}>岗位进度</Typography.Title>
        <Typography.Text type="secondary" className="board-head-sub">
          点一个岗位，打开详情继续准备
        </Typography.Text>
        <Button size="small" className="board-head-action" onClick={() => void navigate('/jobs')}>
          打开岗位库
        </Button>
      </div>
      <div className="workspace-board">
        {BOARD_COLUMNS.map(({ key, title, dot, emptyText }) => {
          const items = board[key]
          return (
            <div
              key={key}
              className={items.length === 0 ? 'board-column is-empty' : 'board-column has-cards'}
            >
              <div className="board-column-head">
                <span className="board-column-title">
                  <span className="board-dot" style={{ background: dotColors[dot] }} />
                  <Typography.Text strong>{title}</Typography.Text>
                </span>
                {key === 'applied' && (
                  <Link to="/applications" className="board-column-link">
                    查看全部投递
                  </Link>
                )}
                <Typography.Text type="secondary" className="board-count tabular-num">
                  {items.length}
                </Typography.Text>
              </div>
              {items.length === 0 ? (
                <div className="board-column-empty">
                  <span>{emptyText}</span>
                  {key === 'unmatched' && (
                    <button type="button" className="board-empty-action" onClick={onFocusComposer}>
                      发 JD
                    </button>
                  )}
                </div>
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
  const summary = card.match_summary
  const meterTotal = summary ? Math.min(6, Math.max(summary.hard_total, 1)) : 0
  const meterOn =
    summary && summary.hard_total > 0
      ? Math.round((summary.hard_covered / summary.hard_total) * meterTotal)
      : 0

  return (
    <Card size="small" hoverable className="board-card" onClick={onOpen}>
      <Typography.Text strong className="board-card-title">
        {card.title ?? '未命名岗位'}
      </Typography.Text>
      {card.salary && (
        <Typography.Text strong className="board-card-salary" style={{ color: token.colorPrimary }}>
          {card.salary}
        </Typography.Text>
      )}
      <MetaList
        items={jobMetaParts({
          company: card.company,
          location: card.location,
          onsite_days_per_week: card.onsite_days_per_week,
          education: card.education,
        })}
      />
      {meterTotal > 0 && (
        <div className="jobcard-meter" aria-hidden="true">
          {Array.from({ length: meterTotal }, (_, index) => (
            <i key={index} className={index < meterOn ? 'is-on' : undefined} />
          ))}
        </div>
      )}
      <div className="board-card-footer">
        <Typography.Text type="secondary" className="tabular-num">
          {formatMatchSummary(summary)}
        </Typography.Text>
        {card.resume_stale && <Tag color="warning">事实库已更新</Tag>}
        {columnKey === 'applied' && <Tag color="processing">{JOB_STAGE[card.stage]}</Tag>}
      </div>
    </Card>
  )
}
