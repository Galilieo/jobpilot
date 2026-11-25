// 经历事实库：草稿逐条确认、修改、删除，确认后发布成新版本。改写只引用已发布的事实。
// 每次修改都带上读取时的草稿修订号；别处改过（409）就刷新列表并提示，不会确认你没看过的内容。
import { CheckOutlined, DeleteOutlined, WarningOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App,
  Button,
  Card,
  Empty,
  List,
  Popconfirm,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ReactElement } from 'react'
import { facts } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import type { DraftItem, Library, Responsibility } from '../../api/types'
import { ITEM_STATUS, RESPONSIBILITY, REVIEW_REASON } from '../../labels'
import { LIBRARY_QUERY } from './queries'

function groupByExperience(items: DraftItem[]): [string, DraftItem[]][] {
  const groups = new Map<string, DraftItem[]>()
  for (const item of items)
    groups.set(item.experience, [...(groups.get(item.experience) ?? []), item])
  return [...groups]
}

export function FactLibrary(): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const library = useQuery({
    queryKey: LIBRARY_QUERY,
    queryFn: ({ signal }) => facts.library(signal),
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: LIBRARY_QUERY })
  const onError = (error: Error) => {
    if (error instanceof ApiError && error.status === 409) {
      message.warning('事实库已在别处更新，已为你刷新，请再看一眼后操作')
    } else {
      message.error(error.message)
    }
    void refresh()
  }

  const change = useMutation({
    mutationFn: ({
      item,
      changes,
    }: {
      item: DraftItem
      changes: { text?: string; responsibility?: Responsibility; confirmed?: boolean }
    }) => facts.update(item.lineage_id, changes, revisionOf(library.data)),
    onSuccess: refresh,
    onError,
  })
  const remove = useMutation({
    mutationFn: (item: DraftItem) => facts.remove(item.lineage_id),
    onSuccess: refresh,
    onError,
  })
  const setResponsibility = useMutation({
    mutationFn: ({
      experience,
      responsibility,
    }: {
      experience: string
      responsibility: Exclude<Responsibility, 'na'>
    }) => facts.setResponsibility(experience, responsibility, revisionOf(library.data)),
    onSuccess: async ({ changed }) => {
      await refresh()
      message.success(`设置了 ${changed} 条的职责程度`)
    },
    onError,
  })
  const confirmClean = useMutation({
    mutationFn: () => facts.confirmClean(revisionOf(library.data)),
    onSuccess: async ({ changed }) => {
      await refresh()
      message.success(changed ? `确认了 ${changed} 条` : '没有可以直接确认的条目')
    },
    onError,
  })
  const publish = useMutation({
    mutationFn: () =>
      facts.commit(library.data?.fact_set?.fact_set_id ?? null, revisionOf(library.data)),
    onSuccess: async ({ version }) => {
      await refresh()
      message.success(`已发布第 ${version} 版事实库，之后的匹配和改写都用这一版`)
    },
    onError,
  })

  if (library.isPending) return <Skeleton active />
  if (library.isError) return <Empty description={library.error.message} />

  const { fact_set: factSet, facts: published, draft } = library.data
  const pending = draft.items.filter((item) => item.status === 'pending').length
  const ready = draft.items.some((item) => item.status !== 'pending')

  return (
    <Space orientation="vertical" size={16} className="full-width">
      <Card
        title="待确认的经历"
        extra={
          <Space wrap>
            <Button
              disabled={pending === 0}
              loading={confirmClean.isPending}
              onClick={() => confirmClean.mutate()}
            >
              确认全部无需核对的条目
            </Button>
            <Button
              type="primary"
              disabled={!ready}
              loading={publish.isPending}
              onClick={() => publish.mutate()}
            >
              发布新版本
            </Button>
          </Space>
        }
      >
        {draft.items.length === 0 ? (
          <Empty description="没有待确认的条目。导入一份简历，或在已发布的事实上修改。" />
        ) : (
          <>
            <Typography.Paragraph type="secondary">
              待确认 {pending} 条。带黄色提示的条目请逐条看过；只有确认过的会进入下一版事实库。
            </Typography.Paragraph>
            {groupByExperience(draft.items).map(([experience, items]) => (
              <section key={experience} className="fact-group">
                <div className="fact-group-head">
                  <Typography.Title level={5}>{experience}</Typography.Title>
                  {items.some((item) =>
                    item.review_reasons.includes('responsibility_unstated'),
                  ) && (
                    <Select<Exclude<Responsibility, 'na'>>
                      size="small"
                      placeholder="没写明职责的都设为…"
                      aria-label={`把「${experience}」里没写明职责的条目都设为`}
                      disabled={setResponsibility.isPending}
                      onChange={(responsibility) =>
                        setResponsibility.mutate({ experience, responsibility })
                      }
                      options={(['own', 'participate', 'lead'] as const).map((value) => ({
                        value,
                        label: RESPONSIBILITY[value],
                      }))}
                    />
                  )}
                </div>
                <List
                  dataSource={items}
                  renderItem={(item) => (
                    <DraftRow
                      item={item}
                      busy={change.isPending || remove.isPending}
                      onChange={(changes) => change.mutate({ item, changes })}
                      onRemove={() => remove.mutate(item)}
                    />
                  )}
                />
              </section>
            ))}
          </>
        )}
      </Card>
      <Card
        title={factSet ? `已发布 · 第 ${factSet.version} 版` : '已发布'}
        extra={<Typography.Text type="secondary">{published.length} 条</Typography.Text>}
      >
        {published.length === 0 ? (
          <Empty description="还没有发布过事实库" />
        ) : (
          groupByExperience(published.map(asReadonlyDraft)).map(([experience, items]) => (
            <section key={experience} className="fact-group">
              <Typography.Title level={5}>{experience}</Typography.Title>
              <ul className="fact-list">
                {items.map((item) => (
                  <li key={item.lineage_id}>
                    {item.text}
                    <Tag className="fact-tag">{RESPONSIBILITY[item.responsibility]}</Tag>
                  </li>
                ))}
              </ul>
            </section>
          ))
        )}
      </Card>
    </Space>
  )
}

function revisionOf(library: Library | undefined): number {
  return library?.draft.revision ?? 0
}

function asReadonlyDraft(fact: Library['facts'][number]): DraftItem {
  return {
    lineage_id: fact.lineage_id,
    status: 'confirmed',
    experience: fact.experience,
    section: null,
    kind: fact.kind,
    responsibility: fact.responsibility,
    text: fact.text,
    source: fact.source === 'user' ? 'user' : 'resume',
    source_quote: fact.source_quote,
    quote_verified: true,
    review_reasons: [],
  }
}

interface DraftRowProps {
  item: DraftItem
  busy: boolean
  onChange: (changes: {
    text?: string
    responsibility?: Responsibility
    confirmed?: boolean
  }) => void
  onRemove: () => void
}

function DraftRow({ item, busy, onChange, onRemove }: DraftRowProps): ReactElement {
  const blocked = item.review_reasons.includes('quote_not_found')
  return (
    <List.Item
      actions={[
        item.status === 'pending' && (
          <Tooltip key="confirm" title={blocked ? '原文里找不到，请先修改文字或删除' : undefined}>
            <Button
              size="small"
              icon={<CheckOutlined />}
              disabled={busy || blocked}
              onClick={() => onChange({ confirmed: true })}
            >
              确认
            </Button>
          </Tooltip>
        ),
        <Popconfirm
          key="remove"
          title="删除这条经历？"
          okText="删除"
          cancelText="保留"
          onConfirm={onRemove}
        >
          <Button
            size="small"
            type="text"
            danger
            icon={<DeleteOutlined />}
            disabled={busy}
            aria-label="删除"
          />
        </Popconfirm>,
      ].filter(Boolean)}
    >
      <div className="fact-row">
        <Typography.Paragraph
          className="fact-text"
          delete={item.status === 'deleted'}
          editable={{
            tooltip: '修改文字',
            onChange: (text) => text.trim() && text !== item.text && onChange({ text }),
          }}
        >
          {item.text}
        </Typography.Paragraph>
        <Space size={6} wrap>
          <Tag color={item.status === 'confirmed' ? 'success' : undefined}>
            {ITEM_STATUS[item.status]}
          </Tag>
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
          {item.source === 'user' && <Tag>你补充的</Tag>}
          {item.review_reasons.map((reason) => (
            <Tag key={reason} color="warning" icon={<WarningOutlined />}>
              {REVIEW_REASON[reason] ?? reason}
            </Tag>
          ))}
        </Space>
        {item.source_quote && item.source_quote !== item.text && (
          <Typography.Text type="secondary" className="fact-quote">
            原文：{item.source_quote}
          </Typography.Text>
        )}
      </div>
    </List.Item>
  )
}
