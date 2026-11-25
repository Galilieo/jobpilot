// 草稿经历分组与单条事实行：白底行 + 左侧 3px 警示色条，合并职责不明提示到组标题，行内操作悬停/聚焦显示。
import { CheckOutlined, DeleteOutlined, UndoOutlined, WarningOutlined } from '@ant-design/icons'
import { Button, Popconfirm, Select, Space, Tag, Tooltip, Typography } from 'antd'
import type { ReactElement } from 'react'
import type { DraftItem, Responsibility } from '../../api/types'
import { HoverRow } from '../../components/HoverRow'
import { UserFilledTag } from '../../components/UserFilledTag'
import { FACT_KIND, ITEM_STATUS, RESPONSIBILITY, REVIEW_REASON } from '../../labels'

interface DraftExperienceGroupProps {
  experience: string
  items: DraftItem[]
  busy: boolean
  batchBusy: boolean
  onBatchResponsibility: (responsibility: Exclude<Responsibility, 'na'>) => void
  onChange: (
    lineageId: string,
    changes: { text?: string; responsibility?: Responsibility; confirmed?: boolean },
  ) => void
  onRemove: (lineageId: string) => void
}

export function DraftExperienceGroup({
  experience,
  items,
  busy,
  batchBusy,
  onBatchResponsibility,
  onChange,
  onRemove,
}: DraftExperienceGroupProps): ReactElement {
  const unstatedCount = items.filter((item) =>
    item.review_reasons.includes('responsibility_unstated'),
  ).length

  return (
    <section className="fact-section">
      <div className="fact-group-head">
        <Space size={8} wrap align="baseline">
          <Typography.Title level={5}>{experience}</Typography.Title>
          {unstatedCount > 0 && (
            <Typography.Text type="secondary" className="fact-group-note">
              {unstatedCount} 条没写明职责程度
            </Typography.Text>
          )}
        </Space>
        {unstatedCount > 0 && (
          <Select<Exclude<Responsibility, 'na'>>
            size="small"
            placeholder="没写明职责的都设为…"
            aria-label={`把「${experience}」里没写明职责的条目都设为`}
            disabled={batchBusy}
            onChange={onBatchResponsibility}
            options={(['own', 'participate', 'lead'] as const).map((value) => ({
              value,
              label: RESPONSIBILITY[value],
            }))}
          />
        )}
      </div>
      <ul className="row-list">
        {items.map((item) => (
          <DraftFactRow
            key={item.lineage_id}
            item={item}
            busy={busy}
            onChange={(changes) => onChange(item.lineage_id, changes)}
            onRemove={() => onRemove(item.lineage_id)}
          />
        ))}
      </ul>
    </section>
  )
}

interface DraftFactRowProps {
  item: DraftItem
  busy: boolean
  onChange: (changes: {
    text?: string
    responsibility?: Responsibility
    confirmed?: boolean
  }) => void
  onRemove: () => void
}

export function DraftFactRow({ item, busy, onChange, onRemove }: DraftFactRowProps): ReactElement {
  const blocked = item.review_reasons.includes('quote_not_found')
  const rowReasons = item.review_reasons.filter((r) => r !== 'responsibility_unstated')
  const showQuote = Boolean(item.source_quote && item.source_quote !== item.text)

  const actions = (
    <>
      {item.status === 'pending' && (
        <Tooltip title={blocked ? '原文里找不到，请先修改文字或删除' : undefined}>
          <Button
            size="small"
            icon={<CheckOutlined />}
            disabled={busy || blocked}
            onClick={() => onChange({ confirmed: true })}
          >
            确认
          </Button>
        </Tooltip>
      )}
      {item.status === 'deleted' && (
        <Button
          size="small"
          icon={<UndoOutlined />}
          disabled={busy}
          onClick={() => onChange({ confirmed: false })}
        >
          撤销删除
        </Button>
      )}
      {item.status !== 'deleted' && (
        <Popconfirm
          title="删除这条经历？"
          okText="删除"
          cancelText="保留"
          okButtonProps={{ danger: true }}
          onConfirm={onRemove}
        >
          <Button
            size="small"
            type="text"
            icon={<DeleteOutlined />}
            disabled={busy}
            aria-label="删除"
          />
        </Popconfirm>
      )}
    </>
  )

  return (
    <HoverRow highlight={item.status === 'pending' ? 'warning' : 'none'} actions={actions}>
      <div className="fact-row">
        <Typography.Paragraph
          className="fact-text"
          delete={item.status === 'deleted'}
          editable={{
            tooltip: '修改文字',
            onChange: (text) => text.trim() && text.trim() !== item.text && onChange({ text }),
          }}
        >
          {item.text}
        </Typography.Paragraph>
        <Space size={8} wrap>
          {item.status !== 'pending' && (
            <Tag color={item.status === 'confirmed' ? 'success' : 'error'}>
              {ITEM_STATUS[item.status]}
            </Tag>
          )}
          <Tag>{FACT_KIND[item.kind]}</Tag>
          {item.responsibility !== 'na' && (
            <Select<Responsibility>
              size="small"
              value={item.responsibility}
              disabled={busy}
              aria-label="职责程度"
              onChange={(responsibility) => onChange({ responsibility })}
              options={(['lead', 'own', 'participate'] as const).map((value) => ({
                value,
                label: RESPONSIBILITY[value],
              }))}
            />
          )}
          {item.source === 'user' && <UserFilledTag label="你补充的" />}
          {rowReasons.map((reason) => (
            <Tag key={reason} color="warning" icon={<WarningOutlined />}>
              {REVIEW_REASON[reason] ?? reason}
            </Tag>
          ))}
        </Space>
        {showQuote && (
          <Typography.Text type="secondary" className="fact-quote">
            原文：{item.source_quote}
          </Typography.Text>
        )}
      </div>
    </HoverRow>
  )
}
