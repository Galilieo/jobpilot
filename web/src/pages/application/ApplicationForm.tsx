// 记录和编辑投递的两个表单。打开时固定所选版本或修订号，避免后台刷新覆盖用户决定。
import { useMutation, useQueries, useQuery } from '@tanstack/react-query'
import { Alert, App, Form, Input, Modal, Select, Skeleton } from 'antd'
import { useRef, type ReactElement } from 'react'
import { applications, tailoring } from '../../api/endpoints'
import { ApiError, newIdempotencyKey } from '../../api/http'
import type { ApplicationRecord, ApplicationStage, VersionSummary } from '../../api/types'
import { APPLICATION_STAGE } from '../../labels'
import { tailorResumeQuery, tailorRunsQuery } from '../../queryKeys'

const STAGES = (Object.keys(APPLICATION_STAGE) as ApplicationStage[]).map((value) => ({
  value,
  label: APPLICATION_STAGE[value],
}))

function today(): string {
  const current = new Date()
  const local = new Date(current.getTime() - current.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

type SubmissionFields = {
  applied_at: string
  channel?: string
  resume_version_id: string
}

type FormCallbacks = { close: () => void; refresh: () => Promise<void> }

export function NewApplicationModal({
  jobId,
  close,
  refresh,
}: FormCallbacks & { jobId: string }): ReactElement {
  const [form] = Form.useForm<SubmissionFields>()
  const { message } = App.useApp()
  const request = useRef({ key: newIdempotencyKey(), payload: '' })
  const runs = useQuery({
    queryKey: tailorRunsQuery(jobId),
    queryFn: ({ signal }) => tailoring.runs(jobId, signal),
    staleTime: 0,
  })
  const finished = runs.data?.filter((run) => run.has_resume) ?? []
  const resumes = useQueries({
    queries: finished.map(({ run_id }) => ({
      queryKey: tailorResumeQuery(run_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => tailoring.resume(run_id, undefined, signal),
    })),
  })
  const create = useMutation({
    mutationFn: (values: SubmissionFields) => {
      const body = { ...values, job_id: jobId, channel: values.channel?.trim() || null }
      const payload = JSON.stringify(body)
      if (request.current.payload && request.current.payload !== payload) {
        request.current.key = newIdempotencyKey()
      }
      request.current.payload = payload
      return applications.create(body, request.current.key)
    },
    onSuccess: async () => {
      close()
      await refresh()
      void message.success('已记录投递')
    },
    onError: (error) => void message.error(error.message),
  })

  const loading = runs.isPending || resumes.some((resume) => resume.isPending)
  const failed = runs.isError || resumes.some((resume) => resume.isError)
  const currentVersion = resumes[0]?.data?.current_version_id
  const options = resumes.flatMap((resume) =>
    (resume.data?.versions ?? []).map((version: VersionSummary) => ({
      value: version.version_id,
      label: `第 ${version.version} 版${version.version_id === currentVersion ? '（当前）' : ''} · ${new Date(version.created_at).toLocaleString('zh-CN')}`,
    })),
  )

  return (
    <Modal
      title="记录投递"
      open
      onCancel={close}
      onOk={form.submit}
      okText="保存投递"
      cancelText="取消"
      confirmLoading={create.isPending}
      okButtonProps={{ disabled: loading || failed || !currentVersion }}
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 2 }} />
      ) : failed ? (
        <Alert type="error" showIcon title="简历版本读取失败，请重试" />
      ) : !currentVersion ? (
        <Alert type="info" showIcon title="这个岗位还没有定制简历，请先生成" />
      ) : (
        <Form
          form={form}
          layout="vertical"
          initialValues={{ applied_at: today(), resume_version_id: currentVersion }}
          onFinish={(values) => create.mutate(values)}
        >
          <Form.Item name="applied_at" label="投递日期" rules={[{ required: true }]}>
            <Input type="date" />
          </Form.Item>
          <Form.Item name="channel" label="投递渠道">
            <Input maxLength={120} placeholder="如官网、内推" />
          </Form.Item>
          <Form.Item name="resume_version_id" label="简历版本" rules={[{ required: true }]}>
            <Select options={options} />
          </Form.Item>
        </Form>
      )}
    </Modal>
  )
}

type EditFields = Omit<SubmissionFields, 'resume_version_id'> & {
  stage: ApplicationStage
  occurred_at: string
}

export function EditApplicationModal({
  record,
  close,
  refresh,
}: FormCallbacks & { record: ApplicationRecord }): ReactElement {
  const [form] = Form.useForm<EditFields>()
  const { message } = App.useApp()
  const stage = Form.useWatch('stage', form)
  const save = useMutation({
    mutationFn: (values: EditFields) =>
      applications.update(record.application_id, {
        expected_revision: record.revision,
        applied_at: values.applied_at,
        channel: values.channel?.trim() || null,
        stage: values.stage,
        occurred_at: values.stage !== record.stage ? values.occurred_at : undefined,
      }),
    onSuccess: async () => {
      close()
      await refresh()
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) {
        close()
        await refresh()
        void message.warning('投递记录刚被别处改过，已重新读取')
      } else {
        void message.error(error.message)
      }
    },
  })

  return (
    <Modal
      title="编辑投递"
      open
      onCancel={close}
      onOk={form.submit}
      okText="保存修改"
      cancelText="取消"
      confirmLoading={save.isPending}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          applied_at: record.applied_at,
          channel: record.channel,
          stage: record.stage,
          occurred_at: today(),
        }}
        onFinish={(values) => save.mutate(values)}
      >
        <Form.Item name="stage" label="投递阶段" rules={[{ required: true }]}>
          <Select options={STAGES} />
        </Form.Item>
        {stage !== undefined && stage !== record.stage && (
          <Form.Item name="occurred_at" label="阶段发生日期" rules={[{ required: true }]}>
            <Input type="date" />
          </Form.Item>
        )}
        <Form.Item name="applied_at" label="投递日期" rules={[{ required: true }]}>
          <Input type="date" />
        </Form.Item>
        <Form.Item name="channel" label="投递渠道">
          <Input maxLength={120} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
