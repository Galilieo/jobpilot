// 岗位基本信息：两列无边框网格，默认 AI 提取不加标签，仅用户改过或填过的标「你填写」。
import { Button, Input, Space, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import type { BasicFields } from '../../api/types'
import { UserFilledTag } from '../../components/UserFilledTag'

const FIELDS: [keyof BasicFields, string][] = [
  ['title', '岗位名称'],
  ['company', '公司'],
  ['location', '地点'],
  ['salary', '薪资'],
  ['onsite_days_per_week', '每周到岗'],
  ['internship_duration', '实习时长'],
  ['education', '学历'],
  ['graduation_window', '毕业时间'],
  ['application_deadline', '截止日期'],
]

export function JobFieldsSection({
  fields,
  overriddenFields,
  saving,
  onSave,
}: {
  fields: BasicFields
  overriddenFields: string[]
  saving: boolean
  onSave: (patch: Partial<BasicFields>) => Promise<unknown>
}): ReactElement {
  return (
    <section>
      <div className="job-fields-grid">
        {FIELDS.map(([key, label]) => (
          <BasicFieldCell
            key={key}
            label={label}
            value={fields[key]}
            overridden={overriddenFields.includes(key)}
            saving={saving}
            onSave={(next) => onSave({ [key]: next })}
          />
        ))}
      </div>
      <Typography.Text type="secondary" className="job-fields-hint">
        未标注的来自 JD 解析；标「你填写」的是你改过或补充的。点击字段可修改或留空。
      </Typography.Text>
    </section>
  )
}

function BasicFieldCell({
  label,
  value,
  overridden,
  saving,
  onSave,
}: {
  label: string
  value: string | null
  overridden: boolean
  saving: boolean
  onSave: (next: string | null) => Promise<unknown>
}): ReactElement {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value ?? '')

  function startEdit(): void {
    setDraft(value ?? '')
    setEditing(true)
  }

  async function commit(): Promise<void> {
    const trimmed = draft.trim() || null
    if (trimmed === value) {
      setEditing(false)
      return
    }
    try {
      await onSave(trimmed)
      setEditing(false)
    } catch {
      // 保留输入框内容，错误提示由外层统一弹出
    }
  }

  return (
    <div className="job-field-cell">
      <div className="job-field-label">
        <Typography.Text type="secondary">{label}</Typography.Text>
        {value && overridden && <UserFilledTag />}
      </div>
      {editing ? (
        <Space.Compact className="full-width">
          <Input
            size="small"
            value={draft}
            disabled={saving}
            placeholder="留空即清除"
            onChange={(event) => setDraft(event.target.value)}
            onPressEnter={() => void commit()}
            onKeyDown={(event) => event.key === 'Escape' && setEditing(false)}
          />
          <Button size="small" loading={saving} onClick={() => void commit()}>
            保存
          </Button>
          <Button size="small" disabled={saving} onClick={() => setEditing(false)}>
            取消
          </Button>
        </Space.Compact>
      ) : (
        <button type="button" className="job-field-value" onClick={startEdit}>
          {value ?? <Typography.Text type="secondary">未填写</Typography.Text>}
        </button>
      )}
    </div>
  )
}
