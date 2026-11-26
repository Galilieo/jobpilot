// 「我的 → 记忆」页：按分组与接口元数据列出 7 栏偏好，支持三种类型编辑、乐观锁冲突刷新和删除确认。
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Input, Select, Skeleton, Space, Tag, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import { memories } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import type { MemoryGroup, MemoryItem, MemoryValue } from '../../api/types'
import { MEMORIES_QUERY } from '../../queryKeys'

const GROUP_TITLES: Array<{ group: MemoryGroup; title: string }> = [
  { group: 'job', title: '求职偏好' },
  { group: 'writing', title: '写作偏好' },
]

function MemoryValueDisplay({ item }: { item: MemoryItem }): ReactElement {
  if (item.deleted) {
    return (
      <div className="memory-value-wrap">
        <Tag color="warning">已删除</Tag>
        <Typography.Text type="secondary">可随时重新填写恢复</Typography.Text>
      </div>
    )
  }
  if (item.value === null) {
    return <Typography.Text type="secondary">未设置</Typography.Text>
  }
  if (Array.isArray(item.value)) {
    return (
      <div className="memory-chips">
        {item.value.map((entry) => (
          <span key={entry} className="memory-chip">
            {entry}
          </span>
        ))}
      </div>
    )
  }
  return <span className="memory-text-value">{item.value}</span>
}

function initialDraft(item: MemoryItem): MemoryValue {
  if (item.type === 'list') return Array.isArray(item.value) ? item.value : []
  return typeof item.value === 'string' ? item.value : ''
}

function normalizeDraft(item: MemoryItem, draft: MemoryValue): MemoryValue | null {
  if (item.type === 'list') {
    const cleaned = Array.from(
      new Set((Array.isArray(draft) ? draft : []).map((s) => s.trim()).filter(Boolean)),
    )
    return cleaned.length > 0 ? cleaned : null
  }
  const text = typeof draft === 'string' ? draft.trim() : ''
  return text ? text : null
}

function memoryLabel(item: MemoryItem): string {
  return item.label ?? item.key
}

function ListLimitHint({ item }: { item: MemoryItem }): ReactElement | null {
  if (!item.max_items && !item.max_length) return null
  return (
    <Typography.Text type="secondary" className="memory-limit-hint">
      {item.max_items ? `最多 ${item.max_items} 项` : ''}
      {item.max_items && item.max_length ? ' · ' : ''}
      {item.max_length ? `每项不超过 ${item.max_length} 字` : ''}
    </Typography.Text>
  )
}

function MemoryInput({
  item,
  draft,
  onChange,
}: {
  item: MemoryItem
  draft: MemoryValue
  onChange: (value: MemoryValue) => void
}): ReactElement {
  const label = memoryLabel(item)
  const text = typeof draft === 'string' ? draft : ''
  const valueType = item.type ?? 'text'
  if (valueType === 'enum') {
    return (
      <Select
        aria-label={label}
        value={text || undefined}
        placeholder={`请选择${label}`}
        options={(item.options ?? []).map((option) => ({ label: option, value: option }))}
        onChange={onChange}
        style={{ minWidth: 220 }}
      />
    )
  }
  if (valueType === 'list') {
    return (
      <div className="memory-list-editor">
        <Select
          mode="tags"
          aria-label={label}
          value={Array.isArray(draft) ? draft : []}
          placeholder={`输入${label}后按回车添加`}
          tokenSeparators={[',', '，', '、']}
          maxCount={item.max_items ?? undefined}
          onChange={onChange}
          style={{ width: '100%' }}
          open={false}
          suffix={null}
        />
        <ListLimitHint item={item} />
      </div>
    )
  }
  const textProps = {
    'aria-label': label,
    value: text,
    maxLength: item.max_length ?? undefined,
    showCount: Boolean(item.max_length),
    onChange: (event: { target: { value: string } }) => onChange(event.target.value),
  }
  // 长文本栏目（如其他求职偏好）用多行输入框，短的用单行。
  return (item.max_length ?? 0) > 120 ? (
    <Input.TextArea {...textProps} autoSize={{ minRows: 2, maxRows: 4 }} />
  ) : (
    <Input {...textProps} />
  )
}

function MemoryEditor({
  item,
  saving,
  onSave,
  onCancel,
}: {
  item: MemoryItem
  saving: boolean
  onSave: (value: MemoryValue) => void
  onCancel: () => void
}): ReactElement {
  const [draft, setDraft] = useState<MemoryValue>(() => initialDraft(item))
  const normalized = normalizeDraft(item, draft)
  const overLength =
    item.type === 'list' &&
    item.max_length &&
    Array.isArray(normalized) &&
    normalized.some((entry) => entry.length > item.max_length!)

  return (
    <div className="memory-editor">
      <MemoryInput item={item} draft={draft} onChange={setDraft} />
      {overLength && (
        <Typography.Text type="danger" className="memory-limit-hint">
          有条目超过 {item.max_length} 个字，请精简后再保存
        </Typography.Text>
      )}
      <Space size={8}>
        <Button
          type="primary"
          size="small"
          loading={saving}
          disabled={!normalized || Boolean(overLength)}
          onClick={() => normalized && onSave(normalized)}
        >
          保存
        </Button>
        <Button size="small" disabled={saving} onClick={onCancel}>
          取消
        </Button>
      </Space>
    </div>
  )
}

function MemoryRow({
  item,
  editing,
  busy,
  onStartEdit,
  onCancelEdit,
  onSave,
  onDelete,
}: {
  item: MemoryItem
  editing: boolean
  busy: boolean
  onStartEdit: () => void
  onCancelEdit: () => void
  onSave: (value: MemoryValue) => void
  onDelete: () => void
}): ReactElement {
  const label = memoryLabel(item)
  const canDelete = item.value !== null && !item.deleted

  return (
    <div className="memory-row" data-testid={`memory-row-${item.key}`}>
      <div className="memory-row-label">{label}</div>
      <div className="memory-row-body">
        {editing ? (
          <MemoryEditor item={item} saving={busy} onSave={onSave} onCancel={onCancelEdit} />
        ) : (
          <MemoryValueDisplay item={item} />
        )}
      </div>
      {!editing && (
        <div className="memory-row-actions">
          <Button size="small" type="link" onClick={onStartEdit}>
            {item.deleted ? '重新填写' : '编辑'}
          </Button>
          {canDelete && (
            <Button size="small" type="link" danger loading={busy} onClick={onDelete}>
              删除
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

// 编辑、删除和 409 冲突处理集中在这里，页面组件只管渲染。
function useMemoryMutations() {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)

  function replaceRow(updated: MemoryItem): void {
    queryClient.setQueryData<MemoryItem[]>(MEMORIES_QUERY, (prev) =>
      prev ? prev.map((row) => (row.key === updated.key ? updated : row)) : prev,
    )
  }

  function handleMutationError(error: unknown): void {
    if (error instanceof ApiError && error.status === 409) {
      setEditingKey(null)
      void queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY })
      void message.warning('这一栏刚被别处改过，已显示最新内容')
      return
    }
    void message.error(error instanceof Error ? error.message : '操作失败，请稍后重试')
  }

  const updateMutation = useMutation({
    mutationFn: ({ item, value }: { item: MemoryItem; value: MemoryValue }) =>
      memories.update(item.key, { expected_revision: item.revision, value }),
    onMutate: ({ item }) => setBusyKey(item.key),
    onSuccess: (updated) => {
      replaceRow(updated)
      setEditingKey(null)
      void message.success('已保存偏好')
    },
    onError: handleMutationError,
    onSettled: () => setBusyKey(null),
  })

  const removeMutation = useMutation({
    mutationFn: (item: MemoryItem) =>
      memories.remove(item.key, { expected_revision: item.revision }),
    onMutate: (item) => setBusyKey(item.key),
    onSuccess: (updated) => {
      replaceRow(updated)
      void message.success('已删除该项记忆')
    },
    onError: handleMutationError,
    onSettled: () => setBusyKey(null),
  })

  function confirmDelete(item: MemoryItem): void {
    modal.confirm({
      title: `确定删除「${memoryLabel(item)}」的记忆吗？`,
      content: '删除后 Agent 不会再参考这一栏偏好，之后随时可以在这里重新填写。',
      okText: '确认删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: () => removeMutation.mutateAsync(item).catch(() => undefined),
    })
  }

  return {
    editingKey,
    setEditingKey,
    busyKey,
    save: (item: MemoryItem, value: MemoryValue) => updateMutation.mutate({ item, value }),
    confirmDelete,
  }
}

export function MemoryList(): ReactElement {
  const query = useQuery({
    queryKey: MEMORIES_QUERY,
    queryFn: ({ signal }) => memories.list(signal),
  })
  const { editingKey, setEditingKey, busyKey, save, confirmDelete } = useMemoryMutations()

  if (query.isPending) return <Skeleton active paragraph={{ rows: 5 }} />
  if (query.isError) {
    return (
      <Alert
        type="error"
        showIcon
        title={query.error.message}
        action={
          <Button size="small" onClick={() => void query.refetch()}>
            重试
          </Button>
        }
      />
    )
  }

  return (
    <Space orientation="vertical" size={16} className="memory-list full-width">
      <Alert type="info" showIcon title="记忆只影响建议和措辞，不会被当作经历写进简历" />
      {GROUP_TITLES.map(({ group, title }) => {
        const rows = query.data.filter((item) => (item.group ?? 'job') === group)
        if (rows.length === 0) return null
        return (
          <Card key={group} size="small" title={title} className="memory-group-card">
            <div className="memory-group-rows">
              {rows.map((item) => (
                <MemoryRow
                  key={item.key}
                  item={item}
                  editing={editingKey === item.key}
                  busy={busyKey === item.key}
                  onStartEdit={() => setEditingKey(item.key)}
                  onCancelEdit={() => setEditingKey(null)}
                  onSave={(value) => save(item, value)}
                  onDelete={() => confirmDelete(item)}
                />
              ))}
            </div>
          </Card>
        )
      })}
    </Space>
  )
}
