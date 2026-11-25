// 经历事实库：草稿逐条确认、补充、修改、删除，确认后发布成新版本。改写只引用已发布的事实。
// 修改已发布的事实会先复制进草稿（标为待确认或待删除），发布后正式生效；409 冲突时刷新并提示。
import { PlusOutlined, UploadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Skeleton, Space, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import { facts } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import type { Library, Responsibility } from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { LIBRARY_QUERY, OVERVIEW_QUERY, TAILORED_RESUMES_QUERY } from '../../queryKeys'
import { AddFactModal } from './AddFactModal'
import { CriteriaCard } from './CriteriaCard'
import { DraftExperienceGroup } from './DraftFactRow'
import { EducationCard } from './EducationCard'
import { PublishedFactsCard } from './PublishedFactsCard'

function groupByExperience<T extends { experience: string }>(items: T[]): [string, T[]][] {
  const groups = new Map<string, T[]>()
  for (const item of items) {
    groups.set(item.experience, [...(groups.get(item.experience) ?? []), item])
  }
  return [...groups]
}

function revisionOf(library: Library | undefined): number {
  return library?.draft.revision ?? 0
}

export function FactLibrary({ onOpenImport }: { onOpenImport?: () => void }): ReactElement {
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
        <EmptyHint
          description={library.error.message}
          action={<Button onClick={() => void library.refetch()}>重试</Button>}
        />
      </Card>
    )
  }

  const { fact_set: factSet, facts: published, criteria, education, draft } = library.data
  const pending = draft.items.filter((item) => item.status === 'pending').length
  const ready =
    draft.items.some((item) => item.status !== 'pending') ||
    draft.criteria.some((item) => item.status !== 'pending') ||
    draft.education.some((item) => item.draft_status !== 'pending')
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
            {onOpenImport && (
              <Button icon={<UploadOutlined />} onClick={onOpenImport}>
                导入简历
              </Button>
            )}
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
          <EmptyHint
            description="没有待确认的条目。导入一份简历、补充事实，或在已发布的事实上修改。"
            action={<Button onClick={() => setAddingFact(true)}>补充事实</Button>}
          />
        ) : (
          <>
            <Typography.Paragraph type="secondary" className="fact-group-intro">
              待确认 <span className="tabular-num">{pending}</span>{' '}
              条。左侧带色条的条目需要确认，只有确认过的会进入下一版事实库。
            </Typography.Paragraph>
            {groupByExperience(draft.items).map(([experience, items]) => (
              <DraftExperienceGroup
                key={experience}
                experience={experience}
                items={items}
                busy={busy}
                batchBusy={setResponsibility.isPending}
                onBatchResponsibility={(responsibility) =>
                  setResponsibility.mutate({ experience, responsibility })
                }
                onChange={(lineageId, changes) => change.mutate({ lineageId, changes })}
                onRemove={(lineageId) => remove.mutate(lineageId)}
              />
            ))}
          </>
        )}
      </Card>
      <CriteriaCard
        published={criteria}
        draft={draft.criteria}
        revision={draft.revision}
        onSuccess={refresh}
        onError={onError}
      />
      <EducationCard
        published={education}
        draft={draft.education}
        revision={draft.revision}
        onSuccess={refresh}
        onError={onError}
      />
      <PublishedFactsCard
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
