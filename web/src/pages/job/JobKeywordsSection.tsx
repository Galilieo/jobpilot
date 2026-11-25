// 技术关键词标签组：点击标签直接编辑，悬停或聚焦显示关闭按钮，末尾「+ 关键词」标签式输入。
import { CloseOutlined, PlusOutlined } from '@ant-design/icons'
import { Button, Input, Popconfirm, Space, Tag, Typography } from 'antd'
import { useState, type ReactElement } from 'react'

export function JobKeywordsSection({
  keywords,
  saving,
  onSave,
}: {
  keywords: string[]
  saving: boolean
  onSave: (next: string[]) => Promise<unknown>
}): ReactElement {
  const [adding, setAdding] = useState(false)
  const [newKeyword, setNewKeyword] = useState('')
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [editingText, setEditingText] = useState('')

  async function addKeyword(): Promise<void> {
    const trimmed = newKeyword.trim()
    if (!trimmed) {
      setAdding(false)
      return
    }
    try {
      await onSave([...keywords, trimmed])
      setNewKeyword('')
      setAdding(false)
    } catch {
      // 保留输入
    }
  }

  async function saveKeywordEdit(index: number): Promise<void> {
    const trimmed = editingText.trim()
    if (!trimmed) {
      setEditingIndex(null)
      return
    }
    if (trimmed === keywords[index]) {
      setEditingIndex(null)
      return
    }
    try {
      const next = keywords.map((kw, i) => (i === index ? trimmed : kw))
      await onSave(next)
      setEditingIndex(null)
    } catch {
      // 保留编辑
    }
  }

  return (
    <section className="job-requirements">
      <Typography.Title level={5}>技术关键词</Typography.Title>
      <Space size={[8, 8]} wrap className="job-keywords">
        {keywords.map((keyword, index) =>
          editingIndex === index ? (
            <Input
              key={`${keyword}-${index}`}
              size="small"
              autoFocus
              className="keyword-inline-input"
              value={editingText}
              disabled={saving}
              onChange={(event) => setEditingText(event.target.value)}
              onPressEnter={() => void saveKeywordEdit(index)}
              onBlur={() => void saveKeywordEdit(index)}
              onKeyDown={(event) => event.key === 'Escape' && setEditingIndex(null)}
            />
          ) : (
            <Tag key={`${keyword}-${index}`} className="keyword-chip">
              <button
                type="button"
                className="keyword-chip-text"
                disabled={saving}
                onClick={() => {
                  setEditingIndex(index)
                  setEditingText(keyword)
                }}
              >
                {keyword}
              </button>
              <Popconfirm
                title={`删除关键词「${keyword}」？`}
                okText="删除"
                cancelText="保留"
                okButtonProps={{ danger: true }}
                onConfirm={() => void onSave(keywords.filter((_, i) => i !== index))}
              >
                <Button
                  type="text"
                  size="small"
                  className="keyword-chip-remove"
                  icon={<CloseOutlined />}
                  aria-label={`删除关键词 ${keyword}`}
                  disabled={saving}
                />
              </Popconfirm>
            </Tag>
          ),
        )}
        {adding ? (
          <Input
            size="small"
            autoFocus
            className="keyword-inline-input"
            value={newKeyword}
            maxLength={40}
            placeholder="输入关键词"
            disabled={saving}
            onChange={(event) => setNewKeyword(event.target.value)}
            onPressEnter={() => void addKeyword()}
            onBlur={() => {
              if (!newKeyword.trim()) setAdding(false)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setNewKeyword('')
                setAdding(false)
              }
            }}
          />
        ) : (
          <Button
            size="small"
            type="dashed"
            icon={<PlusOutlined />}
            disabled={saving}
            onClick={() => setAdding(true)}
          >
            关键词
          </Button>
        )}
      </Space>
    </section>
  )
}
