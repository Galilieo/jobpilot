// 手动补充一条经历事实：直接加入草稿（标记为「你补充的」），发布后才会进入正式版本。
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { App, AutoComplete, Form, Input, Modal, Select, Typography } from 'antd'
import type { ReactElement } from 'react'
import { facts } from '../../api/endpoints'
import type { FactKind, Responsibility } from '../../api/types'
import { FACT_KIND, RESPONSIBILITY } from '../../labels'
import { LIBRARY_QUERY, OVERVIEW_QUERY } from '../../queryKeys'

interface AddFactForm {
  experience: string
  kind: FactKind
  responsibility: Responsibility
  text: string
}

interface AddFactModalProps {
  open: boolean
  experiences: string[]
  onClose: () => void
}

export function AddFactModal({ open, experiences, onClose }: AddFactModalProps): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [form] = Form.useForm<AddFactForm>()

  const submit = useMutation({
    mutationFn: (values: AddFactForm) =>
      facts.add({
        experience: values.experience.trim(),
        kind: values.kind,
        responsibility: values.responsibility,
        text: values.text.trim(),
      }),
    onSuccess: async () => {
      form.resetFields()
      onClose()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: LIBRARY_QUERY }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY }),
      ])
      message.success('已加入草稿，发布新版本后生效')
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  return (
    <Modal
      title="补充经历事实"
      open={open}
      okText="加入草稿"
      cancelText="取消"
      confirmLoading={submit.isPending}
      onCancel={onClose}
      onOk={() => void form.validateFields().then((values) => submit.mutate(values))}
      destroyOnHidden={false}
    >
      <Typography.Paragraph type="secondary">
        手动补充简历里没写到的经历或数字，直接标为已确认并进入草稿，发布版本后即可用于匹配和改写。
      </Typography.Paragraph>
      <Form<AddFactForm>
        form={form}
        layout="vertical"
        initialValues={{ kind: 'action', responsibility: 'own' }}
      >
        <Form.Item
          name="experience"
          label="所属经历"
          rules={[{ required: true, whitespace: true, message: '请填写经历名称' }]}
        >
          <AutoComplete
            options={experiences.map((value) => ({ value }))}
            placeholder="例如：示例公司后端实习 / 校园项目 / 求职条件"
            filterOption={(input, option) =>
              (option?.value ?? '').toLowerCase().includes(input.toLowerCase())
            }
          />
        </Form.Item>
        <Form.Item name="kind" label="事实类型" rules={[{ required: true }]}>
          <Select
            options={(Object.keys(FACT_KIND) as FactKind[]).map((value) => ({
              value,
              label: FACT_KIND[value],
            }))}
          />
        </Form.Item>
        <Form.Item name="responsibility" label="职责程度" rules={[{ required: true }]}>
          <Select
            options={(['lead', 'own', 'participate', 'na'] as Responsibility[]).map((value) => ({
              value,
              label: RESPONSIBILITY[value],
            }))}
          />
        </Form.Item>
        <Form.Item
          name="text"
          label="事实描述"
          rules={[{ required: true, whitespace: true, message: '请填写事实文字' }]}
        >
          <Input.TextArea
            autoSize={{ minRows: 3, maxRows: 6 }}
            maxLength={500}
            placeholder="例如：对查询接口做压测与优化，吞吐量从约 200 QPS 提升到约 800 QPS"
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
