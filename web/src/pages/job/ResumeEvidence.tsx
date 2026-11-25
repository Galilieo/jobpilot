// 依据追溯与建议补入：展示改写条目引用的事实、原文片段和检查结论，以及计划中建议补入的要求。
import { CheckCircleOutlined, ExclamationCircleOutlined, WarningOutlined } from '@ant-design/icons'
import { Card, Empty, Space, Tag, Typography, theme } from 'antd'
import { useState, type ReactElement } from 'react'
import type { CitedFact, PlanBlock, ResumeBullet, TailoredResumeData } from '../../api/types'

interface BulletEntry {
  experienceTitle: string
  bullet: ResumeBullet
}

function collectBullets(resume: TailoredResumeData): BulletEntry[] {
  return resume.content.sections.flatMap((section) =>
    section.entries.flatMap((entry) =>
      entry.bullets.map((bullet) => ({
        experienceTitle: entry.experience ?? (entry.title || '专业技能'),
        bullet,
      })),
    ),
  )
}

function originTag(bullet: ResumeBullet): ReactElement {
  if (bullet.origin === 'fallback') {
    return (
      <Tag color="warning" icon={<WarningOutlined />}>
        已回退原文
      </Tag>
    )
  }
  if (bullet.origin === 'pending') {
    return (
      <Tag color="error" icon={<ExclamationCircleOutlined />}>
        待处理
      </Tag>
    )
  }
  if (bullet.check.issues.length > 0) {
    return (
      <Tag color="warning" icon={<WarningOutlined />}>
        请核对
      </Tag>
    )
  }
  return (
    <Tag color="success" icon={<CheckCircleOutlined />}>
      通过
    </Tag>
  )
}

export function ResumeEvidence({ resume }: { resume: TailoredResumeData }): ReactElement {
  const entries = collectBullets(resume)
  const [selectedId, setSelectedId] = useState<string | null>(entries[0]?.bullet.bullet_id ?? null)

  if (entries.length === 0) {
    return <Empty description="这份简历还没有改写条目。" />
  }

  return (
    <Space orientation="vertical" size={12} className="full-width">
      <Typography.Text type="secondary">
        点击一条改写，查看它引用的事实、原文片段和检查结论。
      </Typography.Text>
      <ul className="row-list">
        {entries.map(({ experienceTitle, bullet }) => {
          const isSelected = bullet.bullet_id === selectedId
          const cited = bullet.fact_ids
            .map((id) => ({ id, fact: resume.facts[id] }))
            .filter((item): item is { id: string; fact: CitedFact } => Boolean(item.fact))
          return (
            <li key={bullet.bullet_id} className="evidence-item">
              <button
                type="button"
                className="evidence-trigger"
                aria-expanded={isSelected}
                onClick={() => setSelectedId(isSelected ? null : bullet.bullet_id)}
              >
                <div className="evidence-head">
                  <Space size={6} wrap>
                    {originTag(bullet)}
                    <Typography.Text type="secondary">{experienceTitle}</Typography.Text>
                    {bullet.edited && <Tag color="blue">已手动修改</Tag>}
                  </Space>
                </div>
                <Typography.Text>{bullet.text}</Typography.Text>
              </button>
              {isSelected && <BulletDetail bullet={bullet} cited={cited} />}
            </li>
          )
        })}
      </ul>
    </Space>
  )
}

function BulletDetail({
  bullet,
  cited,
}: {
  bullet: ResumeBullet
  cited: { id: string; fact: CitedFact }[]
}): ReactElement {
  const { token } = theme.useToken()
  return (
    <Card
      size="small"
      className="evidence-detail"
      style={{ background: token.colorFillQuaternary }}
    >
      <Space orientation="vertical" size={8} className="full-width">
        <CheckSummary bullet={bullet} />
        <div>
          <Typography.Text strong>引用的事实</Typography.Text>
          {cited.length === 0 ? (
            <Typography.Paragraph type="secondary" className="evidence-empty-facts">
              未关联到已发布的事实。
            </Typography.Paragraph>
          ) : (
            <ul className="evidence-facts-list">
              {cited.map(({ id, fact }) => (
                <li key={id}>
                  <div>
                    <Typography.Text>{fact.text}</Typography.Text>{' '}
                    <Tag>{fact.source === 'user' ? '你补充的' : '来自简历'}</Tag>
                    <Typography.Text type="secondary">（{fact.experience}）</Typography.Text>
                  </div>
                  {fact.source_quote && (
                    <Typography.Text type="secondary" className="fact-quote">
                      原文片段：{fact.source_quote}
                    </Typography.Text>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </Space>
    </Card>
  )
}

function CheckSummary({ bullet }: { bullet: ResumeBullet }): ReactElement {
  if (bullet.origin === 'fallback') {
    return (
      <Typography.Text type="warning">检查结论：改写没通过检查，用了事实原文。</Typography.Text>
    )
  }
  if (bullet.origin === 'pending') {
    return (
      <div>
        <Typography.Text type="danger">
          检查结论：待处理。改写没有通过检查且没有可回退的事实原文，需要你手动修改或删除（导出时会自动略过）。
        </Typography.Text>
        {bullet.check.issues.length > 0 && (
          <ul className="evidence-issues">
            {bullet.check.issues.map((issue, index) => (
              <li key={index}>{issue.message}</li>
            ))}
          </ul>
        )}
      </div>
    )
  }
  if (bullet.check.issues.length > 0) {
    return (
      <div>
        <Typography.Text type="warning">检查结论：需要核对以下问题。</Typography.Text>
        <ul className="evidence-issues">
          {bullet.check.issues.map((issue, index) => (
            <li key={index}>{issue.message}</li>
          ))}
        </ul>
      </div>
    )
  }
  const layersText =
    bullet.check.layers === 'program' ? '已通过第一、二层依据检查' : '已通过三层依据检查'
  return <Typography.Text type="success">检查结论：{layersText}。</Typography.Text>
}

export function SuggestAddList({ blocks }: { blocks: PlanBlock[] }): ReactElement {
  const groups = blocks
    .map((block) => ({
      key: block.key,
      experience: block.experience ?? '专业技能',
      items: block.focus.filter((item) => item.base_status === 'suggest_add'),
    }))
    .filter((group) => group.items.length > 0)

  if (groups.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="所选底稿已涵盖可写的重点要求，没有需要额外补入的条目。"
      />
    )
  }

  return (
    <Space orientation="vertical" size={12} className="full-width">
      <Typography.Text type="secondary">
        以下要求在所选底稿中没写到、建议补入（改写或手改时请只使用你确认过的经历）。
      </Typography.Text>
      {groups.map((group) => (
        <section key={group.key}>
          <Typography.Title level={5}>{group.experience}</Typography.Title>
          <ul className="row-list requirement-list">
            {group.items.map((item, index) => (
              <li key={`${item.text}-${index}`}>
                <Space size={6} wrap>
                  <Tag color={item.type === 'hard' ? 'blue' : undefined}>
                    {item.type === 'hard' ? '硬性要求' : '加分项'}
                  </Tag>
                  <span>{item.text}</span>
                </Space>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </Space>
  )
}
