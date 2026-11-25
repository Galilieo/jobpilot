// 岗位信息：基本字段、硬性要求与加分项、关键词、JD 原文。
import { Card, Collapse, Descriptions, Empty, Tag, Typography } from 'antd'
import type { ReactElement } from 'react'
import type { BasicFields, Job } from '../../api/types'

const FIELDS: [keyof BasicFields, string][] = [
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
  const { basic_fields: fields, requirements, keywords } = job.parsed
  const hard = requirements.filter((r) => r.type === 'hard')
  const preferred = requirements.filter((r) => r.type === 'preferred')
  return (
    <Card title="岗位信息">
      <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
        {FIELDS.map(([key, label]) => (
          <Descriptions.Item key={key} label={label}>
            {fields[key] ?? <Typography.Text type="secondary">未填写</Typography.Text>}
          </Descriptions.Item>
        ))}
      </Descriptions>
      <RequirementList title="硬性要求" items={hard.map((r) => r.text)} />
      <RequirementList title="加分项" items={preferred.map((r) => r.text)} />
      {keywords.length > 0 && (
        <div className="job-keywords">
          {keywords.map((keyword) => (
            <Tag key={keyword}>{keyword}</Tag>
          ))}
        </div>
      )}
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
    </Card>
  )
}

function RequirementList({ title, items }: { title: string; items: string[] }): ReactElement {
  return (
    <section className="job-requirements">
      <Typography.Title level={5}>{title}</Typography.Title>
      {items.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />
      ) : (
        <ul className="row-list requirement-list">
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
