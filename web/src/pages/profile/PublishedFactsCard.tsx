// 已发布经历卡片：按经历分组显示，无内部嵌套边框，行内删除按悬停/聚焦出现且用中性色图标。
import { DeleteOutlined } from '@ant-design/icons'
import { Button, Card, Popconfirm, Select, Space, Tag, Typography } from 'antd'
import type { ReactElement } from 'react'
import type { DraftItem, PublishedFact, Responsibility } from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { HoverRow } from '../../components/HoverRow'
import { UserFilledTag } from '../../components/UserFilledTag'
import { FACT_KIND, RESPONSIBILITY } from '../../labels'

function groupByExperience<T extends { experience: string }>(items: T[]): [string, T[]][] {
  const groups = new Map<string, T[]>()
  for (const item of items) {
    groups.set(item.experience, [...(groups.get(item.experience) ?? []), item])
  }
  return [...groups]
}

export function PublishedFactsCard({
  version,
  published,
  draftByLineage,
  busy,
  onEdit,
  onRemove,
}: {
  version: number | null
  published: PublishedFact[]
  draftByLineage: Map<string, DraftItem>
  busy: boolean
  onEdit: (lineageId: string, changes: { text?: string; responsibility?: Responsibility }) => void
  onRemove: (lineageId: string) => void
}): ReactElement {
  return (
    <Card
      title={version !== null ? `已发布 · 第 ${version} 版` : '已发布'}
      extra={
        <Typography.Text type="secondary" className="tabular-num">
          {published.length} 条
        </Typography.Text>
      }
    >
      {published.length === 0 ? (
        <EmptyHint description="还没有发布过事实库" />
      ) : (
        groupByExperience(published).map(([experience, items]) => (
          <section key={experience} className="fact-section">
            <div className="fact-group-head">
              <Typography.Title level={5}>{experience}</Typography.Title>
            </div>
            <ul className="row-list">
              {items.map((fact) => (
                <PublishedRow
                  key={fact.fact_id}
                  fact={fact}
                  draftItem={draftByLineage.get(fact.lineage_id)}
                  busy={busy}
                  onEdit={(changes) => onEdit(fact.lineage_id, changes)}
                  onRemove={() => onRemove(fact.lineage_id)}
                />
              ))}
            </ul>
          </section>
        ))
      )}
    </Card>
  )
}

function PublishedRow({
  fact,
  draftItem,
  busy,
  onEdit,
  onRemove,
}: {
  fact: PublishedFact
  draftItem: DraftItem | undefined
  busy: boolean
  onEdit: (changes: { text?: string; responsibility?: Responsibility }) => void
  onRemove: () => void
}): ReactElement {
  const removeButton = (
    <Popconfirm
      title="在下一版事实库中删除这条事实？"
      description="删除会先进入上方草稿，发布新版本后生效。"
      okText="标记删除"
      cancelText="保留"
      okButtonProps={{ danger: true }}
      onConfirm={onRemove}
    >
      <Button
        size="small"
        type="text"
        icon={<DeleteOutlined />}
        disabled={busy || draftItem?.status === 'deleted'}
        aria-label="删除已发布事实"
      />
    </Popconfirm>
  )

  return (
    <HoverRow actions={removeButton}>
      <div className="fact-row">
        <Typography.Paragraph
          className="fact-text"
          delete={draftItem?.status === 'deleted'}
          editable={{
            tooltip: '修改文字（改完会进入上方待确认区）',
            onChange: (text) => text.trim() && text.trim() !== fact.text && onEdit({ text }),
          }}
        >
          {fact.text}
        </Typography.Paragraph>
        <Space size={8} wrap>
          <Tag>{FACT_KIND[fact.kind]}</Tag>
          <Select<Responsibility>
            size="small"
            value={fact.responsibility}
            disabled={busy}
            aria-label="已发布事实职责程度"
            onChange={(responsibility) => onEdit({ responsibility })}
            options={(['lead', 'own', 'participate', 'na'] as const).map((value) => ({
              value,
              label: RESPONSIBILITY[value],
            }))}
          />
          {fact.source === 'user' && <UserFilledTag label="你补充的" />}
          {draftItem && (
            <Tag color={draftItem.status === 'deleted' ? 'error' : 'processing'}>
              {draftItem.status === 'deleted' ? '草稿中待删除' : '草稿中已修改'}
            </Tag>
          )}
        </Space>
      </div>
    </HoverRow>
  )
}
