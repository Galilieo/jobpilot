// 求职条件卡片：每周到岗天数、可实习月数、最早到岗时间，共用事实库草稿修订号与发布流程。
import {
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  UndoOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useMutation } from '@tanstack/react-query'
import {
  Button,
  Card,
  Input,
  InputNumber,
  Popconfirm,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useState, type ReactElement } from 'react'
import { facts } from '../../api/endpoints'
import type { CriterionField, DraftCriterion, PublishedCriterion } from '../../api/types'
import { CRITERION_LABEL, ITEM_STATUS, REVIEW_REASON } from '../../labels'

const FIELDS: readonly CriterionField[] = [
  'onsite_days_per_week',
  'internship_months',
  'earliest_start',
]

function formatValue(field: CriterionField, intVal: number | null, textVal: string | null): string {
  if (field === 'onsite_days_per_week' && intVal !== null) return `${intVal} 天/周`
  if (field === 'internship_months' && intVal !== null) return `${intVal} 个月`
  if (field === 'earliest_start' && textVal) return textVal
  return '未填写'
}

export function CriteriaCard({
  published,
  draft,
  revision,
  onSuccess,
  onError,
}: {
  published: PublishedCriterion[]
  draft: DraftCriterion[]
  revision: number
  onSuccess: () => Promise<void>
  onError: (error: Error) => void
}): ReactElement {
  const [editingField, setEditingField] = useState<CriterionField | null>(null)
  const [draftInt, setDraftInt] = useState<number | null>(null)
  const [draftText, setDraftText] = useState('')

  const save = useMutation({
    mutationFn: (body: Parameters<typeof facts.updateCriterion>[0]) => facts.updateCriterion(body),
    onSuccess: async (_data, variables) => {
      if (variables.int_value !== undefined || variables.text_value !== undefined) {
        setEditingField(null)
      }
      await onSuccess()
    },
    onError,
  })

  const pubByField = new Map(published.map((item) => [item.field, item]))
  const draftByField = new Map(draft.map((item) => [item.field, item]))

  function beginEdit(field: CriterionField): void {
    const d = draftByField.get(field)
    const p = pubByField.get(field)
    setEditingField(field)
    setDraftInt(d?.int_value ?? p?.int_value ?? null)
    setDraftText(d?.text_value ?? p?.text_value ?? '')
  }

  return (
    <Card
      title="求职条件"
      extra={<Typography.Text type="secondary">匹配直接引用，确认后随事实库发布</Typography.Text>}
    >
      <ul className="row-list">
        {FIELDS.map((field) => {
          const d = draftByField.get(field)
          const p = pubByField.get(field)
          const current = d ?? p
          const isEditing = editingField === field
          const isDeleted = d?.status === 'deleted'
          const blocked = Boolean(d && d.source === 'resume' && !d.quote_verified)
          const needsCheck = d?.status === 'pending' && d.review_reasons.length > 0
          const hasAny = Boolean(current)

          return (
            <li
              key={field}
              className={
                needsCheck || d?.status === 'pending' ? 'fact-item fact-item-warn' : 'fact-item'
              }
            >
              <div className="fact-row">
                <Space size={8} wrap>
                  <Typography.Text strong>{CRITERION_LABEL[field]}</Typography.Text>
                  {!isEditing && (
                    <Typography.Text delete={isDeleted} type={hasAny ? undefined : 'secondary'}>
                      {current
                        ? formatValue(field, current.int_value, current.text_value)
                        : '未填写'}
                    </Typography.Text>
                  )}
                  {d && (
                    <Tag
                      color={
                        d.status === 'confirmed'
                          ? 'success'
                          : d.status === 'deleted'
                            ? 'error'
                            : 'warning'
                      }
                    >
                      {ITEM_STATUS[d.status]}
                    </Tag>
                  )}
                  {!d && p && <Tag color="success">已发布</Tag>}
                  {current?.source === 'user' && <Tag color="processing">你补充的</Tag>}
                  {d?.review_reasons.map((reason) => (
                    <Tag key={reason} color="warning" icon={<WarningOutlined />}>
                      {REVIEW_REASON[reason] ?? reason}
                    </Tag>
                  ))}
                </Space>
                {isEditing && (
                  <Space size={8} wrap>
                    {field === 'earliest_start' ? (
                      <Input
                        size="small"
                        aria-label={CRITERION_LABEL[field]}
                        placeholder="如：12 月中旬、可立即到岗"
                        value={draftText}
                        onChange={(e) => setDraftText(e.target.value)}
                      />
                    ) : (
                      <InputNumber
                        size="small"
                        aria-label={CRITERION_LABEL[field]}
                        min={1}
                        max={field === 'onsite_days_per_week' ? 7 : 24}
                        value={draftInt}
                        onChange={(val) => setDraftInt(val)}
                      />
                    )}
                    <Button
                      size="small"
                      type="primary"
                      loading={save.isPending}
                      disabled={
                        field === 'earliest_start' ? !draftText.trim() : draftInt === null
                      }
                      onClick={() =>
                        save.mutate({
                          field,
                          int_value: field === 'earliest_start' ? null : draftInt,
                          text_value: field === 'earliest_start' ? draftText.trim() : null,
                          confirmed: true,
                          expected_revision: revision,
                        })
                      }
                    >
                      保存并确认
                    </Button>
                    <Button size="small" onClick={() => setEditingField(null)}>
                      取消
                    </Button>
                  </Space>
                )}
                {current?.source_quote && (
                  <Typography.Text type="secondary" className="fact-quote">
                    原文：{current.source_quote}
                  </Typography.Text>
                )}
              </div>
              <div className="fact-actions">
                {d?.status === 'pending' && (
                  <Tooltip title={blocked ? '原文里找不到，请修改后保存' : undefined}>
                    <Button
                      size="small"
                      icon={<CheckOutlined />}
                      disabled={save.isPending || blocked}
                      onClick={() =>
                        save.mutate({ field, confirmed: true, expected_revision: revision })
                      }
                    >
                      确认
                    </Button>
                  </Tooltip>
                )}
                {!isEditing && (
                  <Button
                    size="small"
                    type="text"
                    icon={<EditOutlined />}
                    disabled={save.isPending}
                    aria-label={`编辑${CRITERION_LABEL[field]}`}
                    onClick={() => beginEdit(field)}
                  />
                )}
                {isDeleted ? (
                  <Button
                    size="small"
                    icon={<UndoOutlined />}
                    disabled={save.isPending}
                    onClick={() =>
                      save.mutate({ field, confirmed: false, expected_revision: revision })
                    }
                  >
                    撤销删除
                  </Button>
                ) : (
                  hasAny && (
                    <Popconfirm
                      title={`删除「${CRITERION_LABEL[field]}」？`}
                      okText="删除"
                      cancelText="保留"
                      onConfirm={() =>
                        save.mutate({ field, delete: true, expected_revision: revision })
                      }
                    >
                      <Button
                        size="small"
                        type="text"
                        danger
                        icon={<DeleteOutlined />}
                        disabled={save.isPending}
                        aria-label={`删除${CRITERION_LABEL[field]}`}
                      />
                    </Popconfirm>
                  )
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}
