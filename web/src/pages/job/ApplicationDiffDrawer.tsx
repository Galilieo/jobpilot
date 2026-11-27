// 在同一份简历中显示条目变化，生成过新简历时再并排显示两份正文。
import { useQuery } from '@tanstack/react-query'
import { Alert, Drawer, Skeleton, Typography } from 'antd'
import type { ReactElement } from 'react'
import { applications } from '../../api/endpoints'
import type { ApplicationDiff, TailoredResumeData, TextChange } from '../../api/types'
import { applicationDiffQuery } from '../../queryKeys'

const RISK_LABEL = {
  fact_changed: '引用的事实后来有修改',
  fact_removed: '引用的事实后来被删除',
  bullet_removed: '当前版本已移除这条',
  uses_facts: '当前版本已改用事实原文',
}

export function ApplicationDiffDrawer({
  id,
  close,
}: {
  id: string
  close: () => void
}): ReactElement {
  const diff = useQuery({
    queryKey: applicationDiffQuery(id),
    queryFn: ({ signal }) => applications.diff(id, signal),
  })
  return (
    <Drawer title="投递时和现在对比" size={900} open onClose={close}>
      {diff.isPending && <Skeleton active />}
      {diff.isError && <Alert type="error" title={diff.error.message} />}
      {diff.data && <DiffBody diff={diff.data} id={id} />}
    </Drawer>
  )
}

function DiffBody({ diff, id }: { diff: ApplicationDiff; id: string }): ReactElement {
  return (
    <div className="application-diff">
      <section>
        <Typography.Title level={4}>简历差异</Typography.Title>
        {diff.source_deleted ? (
          <Alert type="info" title="原定制简历已删除，仍可查看当时的快照。" />
        ) : (
          <ChangeList changes={diff.resume_changes} empty="同一份简历的条目没有变化。" />
        )}
        {diff.regenerated && diff.regenerated_content && (
          <>
            <Alert type="info" title="之后重新生成过简历，两份简历并排展示。" />
            <div className="application-comparison">
              <div>
                <Typography.Text strong>投递时选定的简历</Typography.Text>
                <iframe
                  title="投递时选定的简历"
                  className="application-snapshot application-comparison-frame"
                  sandbox=""
                  src={applications.snapshotUrl(id)}
                />
              </div>
              <div>
                <Typography.Text strong>重新生成的简历</Typography.Text>
                <ResumeText content={diff.regenerated_content} />
              </div>
            </div>
          </>
        )}
      </section>
      <section>
        <Typography.Title level={4}>事实库差异</Typography.Title>
        <ChangeList changes={diff.fact_changes} empty="事实文字没有变化。" />
      </section>
      <section>
        <Typography.Title level={4}>风险提醒</Typography.Title>
        {diff.risks.length ? (
          <>
            <Alert type="warning" showIcon title="面试时按当时选定的写法准备，避免进一步夸大。" />
            {diff.risks.map((risk) => (
              <div className="application-risk" key={`${risk.bullet_id}-${risk.reason}`}>
                <Typography.Text strong>{RISK_LABEL[risk.reason]}</Typography.Text>
                <Typography.Paragraph>{risk.bullet_text}</Typography.Paragraph>
                <ChangeList
                  changes={risk.facts}
                  empty="这条简历内容没有对应的事实文字。"
                  labelChanges={false}
                />
              </div>
            ))}
          </>
        ) : (
          <Typography.Text type="secondary">没有发现需要提醒的条目。</Typography.Text>
        )}
      </section>
    </div>
  )
}

function ChangeList({
  changes,
  empty,
  labelChanges = true,
}: {
  changes: TextChange[]
  empty: string
  labelChanges?: boolean
}): ReactElement {
  if (changes.length === 0) return <Typography.Text type="secondary">{empty}</Typography.Text>
  return (
    <div className="application-changes">
      {changes.map((change) => (
        <div key={change.item_id}>
          {labelChanges && (
            <Typography.Text type="secondary">
              {change.before == null ? '新增' : change.after == null ? '删除' : '修改'}
            </Typography.Text>
          )}
          <div className="before-after">
            <div>
              <small>投递时</small>
              <p>{change.before ?? '—'}</p>
            </div>
            <div className="is-after">
              <small>现在</small>
              <p>{change.after ?? '—'}</p>
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function ResumeText({ content }: { content: TailoredResumeData['content'] }): ReactElement {
  return (
    <div className="application-resume-text">
      {content.header.map((line, index) => (
        <div key={index}>{line}</div>
      ))}
      {content.sections.map((section, index) => (
        <section key={index}>
          {section.title && <Typography.Text strong>{section.title}</Typography.Text>}
          {section.entries.map((entry) => (
            <div key={entry.entry_id}>
              <Typography.Text strong>{entry.heading || entry.title}</Typography.Text>
              {entry.lines.map((line, i) => (
                <div key={i}>{line}</div>
              ))}
              {entry.bullets
                .filter((bullet) => bullet.origin !== 'pending')
                .map((bullet) => (
                  <div key={bullet.bullet_id}>{bullet.text.replace(/\*\*(.+?)\*\*/g, '$1')}</div>
                ))}
            </div>
          ))}
        </section>
      ))}
    </div>
  )
}
