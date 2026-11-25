// 经历事实库：草稿逐条确认、补充、修改、删除，确认后发布成新版本。改写只引用已发布的事实。
// 修改已发布的事实会先复制进草稿（标为待确认或待删除），发布后正式生效；409 冲突时刷新并提示。
import {
  CheckOutlined,
  DeleteOutlined,
  PlusOutlined,
  UndoOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App,
  Button,
  Card,
  Empty,
  Popconfirm,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useState, type ReactElement } from 'react'
import { facts } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import type { DraftItem, Library, PublishedFact, Responsibility } from '../../api/types'
import { FACT_KIND, ITEM_STATUS, RESPONSIBILITY, REVIEW_REASON } from '../../labels'
import { LIBRARY_QUERY, OVERVIEW_QUERY, TAILORED_RESUMES_QUERY } from '../../queryKeys'
import { AddFactModal } from './AddFactModal'

function groupByExperience<T extends { experience: string }>(items: T[]): [string, T[]][] {
  const groups = new Map<string, T[]>()
  for (const item of items) {
    groups.set(item.experience, [...(groups.get(item.experience) ?? []), item])
  }
  return [...groups]
}

export function FactLibrary(): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [addingFact, setAddingFact] = useState(false)
  const library = useQuery({
    queryKey: LIBRARY_QUERY,
    queryFn: ({ signal }) => facts.library(signal),
  })

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: LIBRARY_QUERY }),
      queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
    ])
  }
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
      lineageId,
      changes,
    }: {
      lineageId: string
      changes: { text?: string; responsibility?: Responsibility; confirmed?: boolean }
    }) => facts.update(lineageId, changes, revisionOf(library.data)),
    onSuccess: refresh,
    onError,
  })
  const editPublished = useMutation({
    mutationFn: ({
      lineageId,
      changes,
    }: {
      lineageId: string
      changes: { text?: string; responsibility?: Responsibility }
    }) => facts.update(lineageId, changes, revisionOf(library.data)),
    onSuccess: async () => {
      await refresh()
      message.success('已复制到上方待确认区，确认并发布后生效')
    },
    onError,
  })
  const remove = useMutation({
    mutationFn: (lineageId: string) => facts.remove(lineageId, revisionOf(library.data)),
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
      await Promise.all([
        refresh(),
        queryClient.invalidateQueries({ queryKey: TAILORED_RESUMES_QUERY }),
      ])
      message.success(`已发布第 ${version} 版事实库，之后的匹配和改写都用这一版`)
    },
    onError,
  })

  if (library.isPending) return <Skeleton active />
  if (library.isError) {
    return (
      <Card>
        <Empty description={library.error.message}>
          <Button onClick={() => void library.refetch()}>重试</Button>
        </Empty>
      </Card>
    )
  }

  const { fact_set: factSet, facts: published, draft } = library.data
  const pending = draft.items.filter((item) => item.status === 'pending').length
  const ready = draft.items.some((item) => item.status !== 'pending')
  const busy = change.isPending || editPublished.isPending || remove.isPending
  const draftByLineage = new Map(draft.items.map((item) => [item.lineage_id, item]))
  const knownExperiences = [
    ...new Set([
      ...draft.items.map((item) => item.experience),
      ...published.map((item) => item.experience),
    ]),
  ]

  return (
    <Space orientation="vertical" size={16} className="full-width">
      <Card
        title="待确认的经历"
        extra={
          <Space wrap>
            <Button icon={<PlusOutlined />} onClick={() => setAddingFact(true)}>
              补充事实
            </Button>
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
          <Empty description="没有待确认的条目。导入一份简历、补充事实，或在已发布的事实上修改。">
            <Button onClick={() => setAddingFact(true)}>补充事实</Button>
          </Empty>
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
                <ul className="row-list">
                  {items.map((item) => (
                    <DraftRow
                      key={item.lineage_id}
                      item={item}
                      busy={busy}
                      onChange={(changes) => change.mutate({ lineageId: item.lineage_id, changes })}
                      onRemove={() => remove.mutate(item.lineage_id)}
                    />
                  ))}
                </ul>
              </section>
            ))}
          </>
        )}
      </Card>
      <PublishedCard
        version={factSet?.version ?? null}
        published={published}
        draftByLineage={draftByLineage}
        busy={busy}
        onEdit={(lineageId, changes) => editPublished.mutate({ lineageId, changes })}
        onRemove={(lineageId) => remove.mutate(lineageId)}
      />
      <AddFactModal
        open={addingFact}
        experiences={knownExperiences}
        onClose={() => setAddingFact(false)}
      />
    </Space>
  )
}

function PublishedCard({
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
      extra={<Typography.Text type="secondary">{published.length} 条</Typography.Text>}
    >
      {published.length === 0 ? (
        <Empty description="还没有发布过事实库" />
      ) : (
        groupByExperience(published).map(([experience, items]) => (
          <section key={experience} className="fact-group">
            <Typography.Title level={5}>{experience}</Typography.Title>
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
  return (
    <li className="fact-item">
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
        <Space size={6} wrap>
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
          {fact.source === 'user' && <Tag color="processing">你补充的</Tag>}
          {draftItem && (
            <Tag color={draftItem.status === 'deleted' ? 'error' : 'processing'}>
              {draftItem.status === 'deleted' ? '草稿中待删除' : '草稿中已修改'}
            </Tag>
          )}
        </Space>
      </div>
      <div className="fact-actions">
        <Popconfirm
          title="在下一版事实库中删除这条事实？"
          description="删除会先进入上方草稿，发布新版本后生效。"
          okText="标记删除"
          cancelText="保留"
          onConfirm={onRemove}
        >
          <Button
            size="small"
            type="text"
            danger
            icon={<DeleteOutlined />}
            disabled={busy || draftItem?.status === 'deleted'}
            aria-label="删除已发布事实"
          />
        </Popconfirm>
      </div>
    </li>
  )
}

function revisionOf(library: Library | undefined): number {
  return library?.draft.revision ?? 0
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
  const needsCheck = item.status === 'pending' && item.review_reasons.length > 0
  return (
    <li className={needsCheck ? 'fact-item fact-item-warn' : 'fact-item'}>
      <FactBody item={item} busy={busy} onChange={onChange} />
      <div className="fact-actions">
        {[
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
          item.status === 'deleted' && (
            <Button
              key="undo-delete"
              size="small"
              icon={<UndoOutlined />}
              disabled={busy}
              onClick={() => onChange({ confirmed: false })}
            >
              撤销删除
            </Button>
          ),
          item.status !== 'deleted' && (
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
            </Popconfirm>
          ),
        ].filter(Boolean)}
      </div>
    </li>
  )
}

function FactBody({ item, busy, onChange }: Omit<DraftRowProps, 'onRemove'>): ReactElement {
  return (
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
      <Space size={6} wrap>
        <Tag
          color={
            item.status === 'confirmed'
              ? 'success'
              : item.status === 'deleted'
                ? 'error'
                : undefined
          }
        >
          {ITEM_STATUS[item.status]}
        </Tag>
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
        {item.source === 'user' && <Tag color="processing">你补充的</Tag>}
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
  )
}
