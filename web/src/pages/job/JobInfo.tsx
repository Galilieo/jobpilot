// 岗位信息：串联基本字段、职责与要求、技术关键词；另导出注入告警供 JD 原文页使用。
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Divider, Space } from 'antd'
import type { ReactElement } from 'react'
import { jobs } from '../../api/endpoints'
import type { InjectionFlag, Job, JobChanges } from '../../api/types'
import { INJECTION_RULE } from '../../labels'
import { JOBS_QUERY, OVERVIEW_QUERY, TAILORED_RESUMES_QUERY, jobQuery } from '../../queryKeys'
import { JobFieldsSection } from './JobFieldsSection'
import { JobKeywordsSection } from './JobKeywordsSection'
import { JobRequirementsSection } from './JobRequirementsSection'

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
    <Space orientation="vertical" size={16} className="full-width">
      <InjectionAlert flags={job.injection_flags} />
      <JobFieldsSection
        fields={fields}
        overriddenFields={job.overridden_basic_fields}
        saving={save.isPending}
        onSave={(basic_fields) => save.mutateAsync({ basic_fields })}
      />
      <Divider className="section-divider" />
      <JobRequirementsSection
        duties={duties.map((d) => d.text)}
        requirements={requirements}
        saving={save.isPending}
        onSave={(next) => save.mutateAsync({ requirements: next })}
      />
      <Divider className="section-divider" />
      <JobKeywordsSection
        keywords={keywords}
        saving={save.isPending}
        onSave={(next) => save.mutateAsync({ keywords: next })}
      />
    </Space>
  )
}

export function InjectionAlert({ flags }: { flags: InjectionFlag[] }): ReactElement | null {
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
