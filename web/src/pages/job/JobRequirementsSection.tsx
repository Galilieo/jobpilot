// 岗位职责与要求列表：不标「AI 提取」，行内操作（编辑、转分类、删除）收进悬停/聚焦出现的「⋯」菜单。
import { PlusOutlined } from '@ant-design/icons'
import { App, Button, Input, Select, Space, Tag, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import type { Requirement, RequirementChange, RequirementType } from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { HoverRow } from '../../components/HoverRow'
import { UserFilledTag } from '../../components/UserFilledTag'

function toRequirementChanges(items: Requirement[]): RequirementChange[] {
  return items.map((item) => ({
    requirement_id: item.requirement_id,
    type: item.type,
    text: item.text,
  }))
}

export function JobRequirementsSection({
  duties,
  requirements,
  saving,
  onSave,
}: {
  duties: string[]
  requirements: Requirement[]
  saving: boolean
  onSave: (next: RequirementChange[]) => Promise<unknown>
}): ReactElement {
  const { modal } = App.useApp()
  const [newType, setNewType] = useState<RequirementType>('hard')
  const [newText, setNewText] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingText, setEditingText] = useState('')

  const hard = requirements.filter((r) => r.type === 'hard')
  const preferred = requirements.filter((r) => r.type === 'preferred')

  async function updateItem(id: string, patch: Partial<RequirementChange>): Promise<void> {
    const next = toRequirementChanges(requirements).map((item) =>
      item.requirement_id === id ? { ...item, ...patch } : item,
    )
    await onSave(next)
  }

  function removeItem(id: string): void {
    const next = toRequirementChanges(requirements).filter((item) => item.requirement_id !== id)
    void onSave(next).catch(() => undefined)
  }

  function confirmRemove(id: string): void {
    modal.confirm({
      title: '删除这条岗位要求？',
      okText: '删除',
      cancelText: '保留',
      okButtonProps: { danger: true },
      onOk: () => removeItem(id),
    })
  }

  async function saveEdit(id: string): Promise<void> {
    const trimmed = editingText.trim()
    if (!trimmed) return
    try {
      await updateItem(id, { text: trimmed })
      setEditingId(null)
    } catch {
      // 保留输入框内容
    }
  }

  async function addItem(): Promise<void> {
    const text = newText.trim()
    if (!text) return
    try {
      await onSave([...toRequirementChanges(requirements), { type: newType, text }])
      setNewText('')
    } catch {
      // 出错时保留新输入的内容
    }
  }

  function renderGroup(title: string, items: Requirement[], targetType: RequirementType) {
    return (
      <section className="job-requirements">
        <Typography.Title level={5}>{title}</Typography.Title>
        {items.length === 0 ? (
          <EmptyHint compact description="暂无" />
        ) : (
          <ul className="row-list requirement-list">
            {items.map((item) => {
              const isEditing = editingId === item.requirement_id
              return (
                <HoverRow
                  key={item.requirement_id}
                  menuLabel={`操作要求：${item.text}`}
                  menuItems={
                    isEditing
                      ? undefined
                      : [
                          {
                            key: 'edit',
                            label: '编辑',
                            disabled: saving,
                            onClick: () => {
                              setEditingId(item.requirement_id)
                              setEditingText(item.text)
                            },
                          },
                          {
                            key: 'move',
                            label: targetType === 'preferred' ? '移到加分项' : '移到硬性要求',
                            disabled: saving,
                            onClick: () =>
                              void updateItem(item.requirement_id, { type: targetType }).catch(
                                () => undefined,
                              ),
                          },
                          {
                            key: 'delete',
                            label: '删除',
                            disabled: saving,
                            onClick: () => confirmRemove(item.requirement_id),
                          },
                        ]
                  }
                >
                  {isEditing ? (
                    <Space.Compact className="full-width">
                      <Input
                        size="small"
                        value={editingText}
                        disabled={saving}
                        onChange={(event) => setEditingText(event.target.value)}
                        onPressEnter={() => void saveEdit(item.requirement_id)}
                        onKeyDown={(event) => event.key === 'Escape' && setEditingId(null)}
                      />
                      <Button
                        size="small"
                        loading={saving}
                        onClick={() => void saveEdit(item.requirement_id)}
                      >
                        保存
                      </Button>
                      <Button size="small" disabled={saving} onClick={() => setEditingId(null)}>
                        取消
                      </Button>
                    </Space.Compact>
                  ) : (
                    <Space size={8} wrap>
                      <Typography.Text>{item.text}</Typography.Text>
                      {!item.source_quote && <UserFilledTag />}
                      {item.source_quote && item.quote_verified === false && (
                        <Tag color="warning">原文未找到片段</Tag>
                      )}
                    </Space>
                  )}
                </HoverRow>
              )
            })}
          </ul>
        )}
      </section>
    )
  }

  return (
    <Space orientation="vertical" size={16} className="full-width">
      {duties.length > 0 && (
        <section className="job-requirements">
          <Typography.Title level={5}>岗位职责</Typography.Title>
          <ul className="row-list requirement-list">
            {duties.map((duty, index) => (
              <li key={`${index}-${duty}`}>{duty}</li>
            ))}
          </ul>
        </section>
      )}
      {renderGroup('硬性要求', hard, 'preferred')}
      {renderGroup('加分项', preferred, 'hard')}
      <div className="job-add-row">
        <Select<RequirementType>
          size="small"
          aria-label="新增要求类型"
          value={newType}
          onChange={setNewType}
          options={[
            { value: 'hard', label: '硬性要求' },
            { value: 'preferred', label: '加分项' },
          ]}
        />
        <Input
          size="small"
          value={newText}
          maxLength={300}
          placeholder="补充一条岗位要求"
          onChange={(event) => setNewText(event.target.value)}
          onPressEnter={() => void addItem()}
        />
        <Button
          size="small"
          icon={<PlusOutlined />}
          disabled={!newText.trim()}
          loading={saving}
          onClick={() => void addItem()}
        >
          添加要求
        </Button>
      </div>
    </Space>
  )
}
