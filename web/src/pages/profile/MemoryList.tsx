import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Input,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Space,
  Typography,
} from 'antd'
import { useState, type ReactElement } from 'react'
import { Link } from 'react-router'
import { memories, writingPreference } from '../../api/endpoints'
import type { MemoryCategory, MemoryInput, MemoryItem, WritingField } from '../../api/types'
import { MEMORIES_QUERY, WRITING_PREFERENCE_QUERY } from '../../queryKeys'

const categories: Record<MemoryCategory, string> = {
  job_pref: '求职偏好',
  agent_rule: '对 Agent 的要求',
}

const writingFields: WritingField[] = ['tone', 'bullet_length', 'tech_terms']

function WritingCard(): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: WRITING_PREFERENCE_QUERY,
    queryFn: ({ signal }) => writingPreference.get(signal),
  })
  const update = useMutation({
    mutationFn: writingPreference.update,
    onSuccess: (value) => queryClient.setQueryData(WRITING_PREFERENCE_QUERY, value),
    onError: (error) => {
      void message.error(error.message)
      void query.refetch()
    },
  })
  const preference = query.data
  return (
    <Card title="写作偏好">
      {query.isPending && <Skeleton active paragraph={{ rows: 2 }} />}
      {query.isError && <Alert type="error" showIcon title={query.error.message} />}
      {preference && (
        <Space orientation="vertical" size={12}>
          <Typography.Text type="secondary">
            新建改写会使用这些选项。对话从下一轮起生效。
          </Typography.Text>
          {writingFields.map((field) => (
            <Space key={field} wrap>
              <Typography.Text>{preference.labels[field]}</Typography.Text>
              <Segmented
                aria-label={preference.labels[field]}
                name={`writing-preference-${field}`}
                options={[{ label: '默认', value: '' }, ...preference.options[field]]}
                value={preference.values[field] ?? ''}
                disabled={update.isPending}
                onChange={(value) =>
                  update.mutate({ expected_revision: preference.revision, [field]: value || null })
                }
              />
            </Space>
          ))}
        </Space>
      )}
    </Card>
  )
}

function MemoryEditor({
  initial,
  pending,
  save,
  close,
}: {
  initial?: MemoryItem
  pending: boolean
  save: (body: MemoryInput) => void
  close: () => void
}): ReactElement {
  const [category, setCategory] = useState<MemoryCategory>(initial?.category ?? 'job_pref')
  const [content, setContent] = useState(initial?.content ?? '')
  return (
    <Space orientation="vertical" className="full-width">
      <Select
        aria-label="记忆类别"
        value={category}
        options={Object.entries(categories).map(([value, label]) => ({ value, label }))}
        onChange={setCategory}
      />
      <Input.TextArea
        aria-label="记忆内容"
        value={content}
        maxLength={200}
        showCount
        autoSize={{ minRows: 2 }}
        onChange={(event) => setContent(event.target.value)}
      />
      <Space>
        <Button
          type="primary"
          loading={pending}
          disabled={!content.trim()}
          onClick={() => save({ category, content })}
        >
          保存
        </Button>
        <Button onClick={close} disabled={pending}>
          取消
        </Button>
      </Space>
    </Space>
  )
}

function MemorySource({ item }: { item: MemoryItem }): ReactElement {
  if (item.source.kind === 'manual') {
    return <Typography.Text type="secondary">手动添加</Typography.Text>
  }
  const date = new Date(item.updated_at).toLocaleDateString('en-CA', {
    month: '2-digit',
    day: '2-digit',
  })
  return (
    <Typography.Text type="secondary">
      {item.conversation_title ? (
        <Link to={`/conversations/${item.source.conversation_id}`}>
          来自《{item.conversation_title}》
        </Link>
      ) : (
        '来自已删除的对话'
      )}
      {' · '}
      {date}
    </Typography.Text>
  )
}

export function MemoryList(): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<string | null>(null)
  const query = useQuery({
    queryKey: MEMORIES_QUERY,
    queryFn: ({ signal }) => memories.list(signal),
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY })
  const onError = (error: Error) => {
    void message.error(error.message)
    void refresh()
  }
  const save = useMutation({
    mutationFn: ({ input, item }: { input: MemoryInput; item?: MemoryItem }) =>
      item
        ? memories.update(item.memory_id, { ...input, expected_revision: item.revision })
        : memories.add(input),
    onSuccess: async (result) => {
      if (result.status === 'exists') void message.info('已存在相同的记忆')
      setEditing(null)
      await refresh()
    },
    onError,
  })
  const remove = useMutation({
    mutationFn: (item: MemoryItem) =>
      memories.remove(item.memory_id, { expected_revision: item.revision }),
    onSuccess: refresh,
    onError,
  })
  const data = query.data
  const pending = save.isPending || remove.isPending

  return (
    <Space orientation="vertical" size={16} className="full-width">
      <WritingCard />
      <Card
        title="记忆"
        extra={
          <Button disabled={pending || !data} onClick={() => setEditing('new')}>
            手动添加
          </Button>
        }
      >
        <Space orientation="vertical" size={16} className="full-width">
          <Typography.Text type="secondary">页面改动从下一轮起生效。</Typography.Text>
          {query.isPending && <Skeleton active paragraph={{ rows: 2 }} />}
          {query.isError && <Alert type="error" showIcon title={query.error.message} />}
          {data && (
            <>
              <Typography.Text>
                已用 {data.used_chars} / {data.budget_chars} 字
              </Typography.Text>
              {data.used_chars > 0.8 * data.budget_chars && (
                <Alert type="warning" showIcon title="记忆快满了，可以合并或删除部分条目。" />
              )}
              {editing === 'new' && (
                <MemoryEditor
                  key="new"
                  pending={pending}
                  close={() => setEditing(null)}
                  save={(input) => save.mutate({ input })}
                />
              )}
              {data.entries.length === 0 && (
                <Typography.Text type="secondary">
                  在对话里说出你的偏好或对我的要求，我会记下来；也可以手动添加。
                </Typography.Text>
              )}
              {(Object.keys(categories) as MemoryCategory[]).map((category) => {
                const entries = data.entries.filter((item) => item.category === category)
                if (!entries.length) return null
                return (
                  <section key={category}>
                    <Typography.Title level={5}>{categories[category]}</Typography.Title>
                    <ul className="row-list">
                      {entries.map((item) => (
                        <li key={item.memory_id}>
                          {editing === item.memory_id ? (
                            <MemoryEditor
                              initial={item}
                              pending={pending}
                              close={() => setEditing(null)}
                              save={(input) => save.mutate({ input, item })}
                            />
                          ) : (
                            <Space wrap>
                              <Typography.Text>{item.content}</Typography.Text>
                              <MemorySource item={item} />
                              <Button
                                type="text"
                                size="small"
                                disabled={pending}
                                onClick={() => setEditing(item.memory_id)}
                              >
                                编辑
                              </Button>
                              <Popconfirm
                                title="删除这条记忆？"
                                okText="删除"
                                cancelText="保留"
                                onConfirm={() => remove.mutate(item)}
                              >
                                <Button type="text" size="small" danger disabled={pending}>
                                  删除
                                </Button>
                              </Popconfirm>
                            </Space>
                          )}
                        </li>
                      ))}
                    </ul>
                  </section>
                )
              })}
            </>
          )}
        </Space>
      </Card>
    </Space>
  )
}
