// 岗位信息：注入提醒、可逐格点击修改的基本字段、职责、可增删改的要求与关键词、JD 原文。
import { DeleteOutlined, EditOutlined, PlusOutlined, SwapOutlined } from '@ant-design/icons'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd'
import { useState, type ReactElement } from 'react'
import { jobs } from '../../api/endpoints'
import type {
  BasicFields,
  InjectionFlag,
  Job,
  JobChanges,
  Requirement,
  RequirementChange,
  RequirementType,
} from '../../api/types'
import { INJECTION_RULE } from '../../labels'
import { JOBS_QUERY, OVERVIEW_QUERY, TAILORED_RESUMES_QUERY, jobQuery } from '../../queryKeys'

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

export function JobInfo({ job }: { job: Job }): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { basic_fields: fields, requirements, duties, keywords } = job.parsed

  const save = useMutation({
    mutationFn: (changes: JobChanges) => jobs.update(job.job_id, changes),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: jobQuery(job.job_id) }),
        queryClient.invalidateQueries({ queryKey: JOBS_QUERY }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
        queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
      ])
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  return (
    <Card title="岗位信息">
      <Space orientation="vertical" size={16} className="full-width">
        <InjectionAlert flags={job.injection_flags} />
        <div>
          <div className="job-fields-grid">
            {FIELDS.map(([key, label]) => (
              <BasicFieldCell
                key={key}
                label={label}
                value={fields[key]}
                overridden={job.overridden_basic_fields.includes(key)}
                saving={save.isPending}
                onSave={(next) => save.mutateAsync({ basic_fields: { [key]: next } })}
              />
            ))}
          </div>
          <Typography.Text type="secondary" className="job-fields-hint">
            点击任意一格即可修改或留空，修改只保存在你的岗位库里。
          </Typography.Text>
        </div>
        {duties.length > 0 && <DutiesList duties={duties.map((d) => d.text)} />}
        <RequirementsEditor
          requirements={requirements}
          saving={save.isPending}
          onSave={(next) => save.mutateAsync({ requirements: next })}
        />
        <KeywordsEditor
          keywords={keywords}
          saving={save.isPending}
          onSave={(next) => save.mutateAsync({ keywords: next })}
        />
        <Collapse
          ghost
          items={[
            {
              key: 'raw',
              label: 'JD 原文',
              children: (
                <Typography.Paragraph className="job-raw">{job.raw_text}</Typography.Paragraph>
              ),
            },
          ]}
        />
      </Space>
    </Card>
  )
}

function InjectionAlert({ flags }: { flags: InjectionFlag[] }): ReactElement | null {
  if (flags.length === 0) return null
  return (
    <Alert
      type="warning"
      showIcon
      title="JD 原文包含疑似提示词注入指令"
      description={
        <Space orientation="vertical" size={4}>
          <span>解析、匹配和改写时已将其隔离为不可信数据，不会执行这些指令，但请留意核对。</span>
          <ul className="job-injection-list">
            {flags.map((flag, index) => (
              <li key={`${flag.start}-${index}`}>
                {INJECTION_RULE[flag.rule] ?? flag.rule}：「{flag.context}」
              </li>
            ))}
          </ul>
        </Space>
      }
    />
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
        {value && (
          <Tag color={overridden ? 'processing' : undefined}>
            {overridden ? '你填写' : 'AI 提取'}
          </Tag>
        )}
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

function DutiesList({ duties }: { duties: string[] }): ReactElement {
  return (
    <section className="job-requirements">
      <Typography.Title level={5}>岗位职责</Typography.Title>
      <ul className="row-list requirement-list">
        {duties.map((duty, index) => (
          <li key={`${index}-${duty}`}>{duty}</li>
        ))}
      </ul>
    </section>
  )
}

function toRequirementChanges(items: Requirement[]): RequirementChange[] {
  return items.map((item) => ({
    requirement_id: item.requirement_id,
    type: item.type,
    text: item.text,
  }))
}

function RequirementsEditor({
  requirements,
  saving,
  onSave,
}: {
  requirements: Requirement[]
  saving: boolean
  onSave: (next: RequirementChange[]) => Promise<unknown>
}): ReactElement {
  const [newType, setNewType] = useState<RequirementType>('hard')
  const [newText, setNewText] = useState('')

  const hard = requirements.filter((r) => r.type === 'hard')
  const preferred = requirements.filter((r) => r.type === 'preferred')

  function updateItem(id: string, patch: Partial<RequirementChange>): void {
    const next = toRequirementChanges(requirements).map((item) =>
      item.requirement_id === id ? { ...item, ...patch } : item,
    )
    void onSave(next).catch(() => undefined)
  }

  function removeItem(id: string): void {
    const next = toRequirementChanges(requirements).filter((item) => item.requirement_id !== id)
    void onSave(next).catch(() => undefined)
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

  return (
    <Space orientation="vertical" size={12} className="full-width">
      <RequirementGroup
        title="硬性要求"
        items={hard}
        saving={saving}
        onEdit={(id, text) => updateItem(id, { text })}
        onToggleType={(id) => updateItem(id, { type: 'preferred' })}
        onDelete={removeItem}
      />
      <RequirementGroup
        title="加分项"
        items={preferred}
        saving={saving}
        onEdit={(id, text) => updateItem(id, { text })}
        onToggleType={(id) => updateItem(id, { type: 'hard' })}
        onDelete={removeItem}
      />
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

function RequirementGroup({
  title,
  items,
  saving,
  onEdit,
  onToggleType,
  onDelete,
}: {
  title: string
  items: Requirement[]
  saving: boolean
  onEdit: (id: string, text: string) => void
  onToggleType: (id: string) => void
  onDelete: (id: string) => void
}): ReactElement {
  return (
    <section className="job-requirements">
      <Typography.Title level={5}>{title}</Typography.Title>
      {items.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />
      ) : (
        <ul className="row-list requirement-list">
          {items.map((item) => (
            <li key={item.requirement_id} className="fact-item">
              <div className="fact-row">
                <Typography.Paragraph
                  className="fact-text"
                  editable={{
                    tooltip: '修改要求文字',
                    onChange: (text) => {
                      const trimmed = text.trim()
                      if (trimmed && trimmed !== item.text) onEdit(item.requirement_id, trimmed)
                    },
                  }}
                >
                  {item.text}
                </Typography.Paragraph>
                <Space size={4} wrap>
                  <Tag color={item.source_quote ? undefined : 'processing'}>
                    {item.source_quote ? 'AI 提取' : '你填写'}
                  </Tag>
                  {item.source_quote && item.quote_verified === false && (
                    <Tag color="warning">原文未找到片段</Tag>
                  )}
                </Space>
              </div>
              <div className="fact-actions">
                <Button
                  size="small"
                  type="text"
                  icon={<SwapOutlined />}
                  disabled={saving}
                  title={item.type === 'hard' ? '转为加分项' : '转为硬性要求'}
                  aria-label={item.type === 'hard' ? '转为加分项' : '转为硬性要求'}
                  onClick={() => onToggleType(item.requirement_id)}
                />
                <Popconfirm
                  title="删除这条岗位要求？"
                  okText="删除"
                  cancelText="保留"
                  onConfirm={() => onDelete(item.requirement_id)}
                >
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={saving}
                    aria-label="删除要求"
                  />
                </Popconfirm>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function KeywordsEditor({
  keywords,
  saving,
  onSave,
}: {
  keywords: string[]
  saving: boolean
  onSave: (next: string[]) => Promise<unknown>
}): ReactElement {
  const [newKeyword, setNewKeyword] = useState('')
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [editingText, setEditingText] = useState('')

  async function addKeyword(): Promise<void> {
    const trimmed = newKeyword.trim()
    if (!trimmed) return
    try {
      await onSave([...keywords, trimmed])
      setNewKeyword('')
    } catch {
      // 保留输入
    }
  }

  async function saveKeywordEdit(index: number): Promise<void> {
    const trimmed = editingText.trim()
    if (!trimmed) return
    try {
      const next = keywords.map((kw, i) => (i === index ? trimmed : kw))
      await onSave(next)
      setEditingIndex(null)
    } catch {
      // 保留编辑
    }
  }

  return (
    <section>
      <Typography.Title level={5}>技术关键词</Typography.Title>
      <Space size={[6, 8]} wrap className="job-keywords">
        {keywords.length === 0 && <Typography.Text type="secondary">暂无关键词</Typography.Text>}
        {keywords.map((keyword, index) =>
          editingIndex === index ? (
            <Space.Compact key={`${keyword}-${index}`} size="small">
              <Input
                size="small"
                value={editingText}
                disabled={saving}
                onChange={(event) => setEditingText(event.target.value)}
                onPressEnter={() => void saveKeywordEdit(index)}
                onKeyDown={(event) => event.key === 'Escape' && setEditingIndex(null)}
              />
              <Button size="small" loading={saving} onClick={() => void saveKeywordEdit(index)}>
                保存
              </Button>
              <Button size="small" disabled={saving} onClick={() => setEditingIndex(null)}>
                取消
              </Button>
            </Space.Compact>
          ) : (
            <Tag key={`${keyword}-${index}`} className="keyword-tag">
              <span>{keyword}</span>
              <Button
                type="text"
                size="small"
                icon={<EditOutlined />}
                aria-label={`编辑关键词 ${keyword}`}
                disabled={saving}
                onClick={() => {
                  setEditingIndex(index)
                  setEditingText(keyword)
                }}
              />
              <Popconfirm
                title={`删除关键词「${keyword}」？`}
                okText="删除"
                cancelText="保留"
                onConfirm={() => void onSave(keywords.filter((_, i) => i !== index))}
              >
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  aria-label={`删除关键词 ${keyword}`}
                  disabled={saving}
                />
              </Popconfirm>
            </Tag>
          ),
        )}
      </Space>
      <div className="job-add-row">
        <Input
          size="small"
          value={newKeyword}
          maxLength={40}
          placeholder="新增关键词，例如 PostgreSQL"
          onChange={(event) => setNewKeyword(event.target.value)}
          onPressEnter={() => void addKeyword()}
        />
        <Button
          size="small"
          icon={<PlusOutlined />}
          disabled={!newKeyword.trim()}
          loading={saving}
          onClick={() => void addKeyword()}
        >
          添加关键词
        </Button>
      </div>
    </section>
  )
}
