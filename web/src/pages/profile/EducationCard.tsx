// 教育经历卡片：一段一行，与事实行共用行内样式，整条确认并支持增删改。
import {
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  UndoOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useMutation } from '@tanstack/react-query'
import {
  Button,
  Card,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useState, type ReactElement } from 'react'
import { facts } from '../../api/endpoints'
import type {
  DegreeLevel,
  DraftEducation,
  EducationStatus,
  PublishedEducation,
} from '../../api/types'
import { EmptyHint } from '../../components/EmptyHint'
import { HoverRow } from '../../components/HoverRow'
import { UserFilledTag } from '../../components/UserFilledTag'
import { DEGREE_LABEL, EDUCATION_STATUS_LABEL, ITEM_STATUS, REVIEW_REASON } from '../../labels'

interface EduForm {
  school: string
  major: string
  degree: DegreeLevel
  status: EducationStatus
  start_text: string
  end_text: string
}

const EMPTY_FORM: EduForm = {
  school: '',
  major: '',
  degree: 'bachelor',
  status: 'enrolled',
  start_text: '',
  end_text: '',
}

function mergeEducationRows(
  published: PublishedEducation[],
  draft: DraftEducation[],
): {
  id: string
  published?: PublishedEducation
  draft?: DraftEducation
}[] {
  const draftById = new Map(draft.map((d) => [d.education_id, d]))
  const rows: { id: string; published?: PublishedEducation; draft?: DraftEducation }[] = []
  const seen = new Set<string>()
  for (const p of published) {
    seen.add(p.education_id)
    rows.push({ id: p.education_id, published: p, draft: draftById.get(p.education_id) })
  }
  for (const d of draft) {
    if (!seen.has(d.education_id)) {
      rows.push({ id: d.education_id, draft: d })
    }
  }
  return rows
}

export function EducationCard({
  published,
  draft,
  revision,
  onSuccess,
  onError,
}: {
  published: PublishedEducation[]
  draft: DraftEducation[]
  revision: number
  onSuccess: () => Promise<void>
  onError: (error: Error) => void
}): ReactElement {
  const [modalOpen, setModalOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [form, setForm] = useState<EduForm>(EMPTY_FORM)

  const add = useMutation({
    mutationFn: (body: Parameters<typeof facts.addEducation>[0]) => facts.addEducation(body),
    onSuccess: async () => {
      setModalOpen(false)
      setForm(EMPTY_FORM)
      await onSuccess()
    },
    onError,
  })

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof facts.updateEducation>[1] }) =>
      facts.updateEducation(id, body),
    onSuccess: async (_data, variables) => {
      if (variables.body.school !== undefined) {
        setModalOpen(false)
        setEditingId(null)
      }
      await onSuccess()
    },
    onError,
  })

  const remove = useMutation({
    mutationFn: (id: string) => facts.removeEducation(id, revision),
    onSuccess,
    onError,
  })

  const rows = mergeEducationRows(published, draft)
  const busy = add.isPending || update.isPending || remove.isPending

  function openNew(): void {
    setEditingId(null)
    setForm(EMPTY_FORM)
    setModalOpen(true)
  }

  function openEdit(row: PublishedEducation | DraftEducation): void {
    setEditingId(row.education_id)
    setForm({
      school: row.school,
      major: row.major ?? '',
      degree: row.degree,
      status: row.status ?? 'enrolled',
      start_text: row.start_text ?? '',
      end_text: row.end_text ?? '',
    })
    setModalOpen(true)
  }

  function submitForm(): void {
    const school = form.school.trim()
    if (!school) return
    const payload = {
      school,
      major: form.major.trim() || null,
      degree: form.degree,
      status: form.status,
      start_text: form.start_text.trim() || null,
      end_text: form.end_text.trim() || null,
      expected_revision: revision,
    }
    if (editingId) {
      update.mutate({ id: editingId, body: { ...payload, confirmed: true } })
    } else {
      add.mutate(payload)
    }
  }

  return (
    <Card
      title="教育"
      extra={
        <Button size="small" icon={<PlusOutlined />} onClick={openNew}>
          添加教育
        </Button>
      }
    >
      {rows.length === 0 ? (
        <EmptyHint description="暂无教育经历，上传简历后自动拆出或点击右上角添加" />
      ) : (
        <ul className="row-list">
          {rows.map(({ id, published: p, draft: d }) => {
            const cur = d ?? p!
            const isDeleted = d?.draft_status === 'deleted'
            const isPending = d?.draft_status === 'pending'
            const blocked = Boolean(
              d && ((d.source === 'resume' && !d.quote_verified) || d.status === null),
            )
            const period = [cur.start_text, cur.end_text].filter(Boolean).join('–')
            const summary = `${cur.school}${cur.major ? ` · ${cur.major}` : ''} · ${DEGREE_LABEL[cur.degree]}`

            const primaryAction = isPending ? (
              <Tooltip title={blocked ? '请先核对就读状态或修改后确认' : undefined}>
                <Button
                  size="small"
                  icon={<CheckOutlined />}
                  disabled={busy || blocked}
                  onClick={() =>
                    update.mutate({
                      id,
                      body: { confirmed: true, expected_revision: revision },
                    })
                  }
                >
                  确认
                </Button>
              </Tooltip>
            ) : isDeleted ? (
              <Button
                size="small"
                icon={<UndoOutlined />}
                disabled={busy}
                onClick={() =>
                  update.mutate({
                    id,
                    body: { confirmed: false, expected_revision: revision },
                  })
                }
              >
                撤销删除
              </Button>
            ) : undefined

            const actions = (
              <>
                <Button
                  size="small"
                  type="text"
                  icon={<EditOutlined />}
                  disabled={busy}
                  aria-label={`编辑${cur.school}`}
                  onClick={() => openEdit(cur)}
                />
                {!isDeleted && (
                  <Popconfirm
                    title={`删除「${cur.school}」这段教育经历？`}
                    okText="删除"
                    cancelText="保留"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => remove.mutate(id)}
                  >
                    <Button
                      size="small"
                      type="text"
                      icon={<DeleteOutlined />}
                      disabled={busy}
                      aria-label={`删除${cur.school}`}
                    />
                  </Popconfirm>
                )}
              </>
            )

            return (
              <HoverRow
                key={id}
                highlight={isPending ? 'warning' : 'none'}
                primaryAction={primaryAction}
                actions={actions}
              >
                <div className="fact-row">
                  <Typography.Paragraph className="fact-text" delete={isDeleted}>
                    {summary}
                    {period ? ` · ${period}` : ''}
                  </Typography.Paragraph>
                  <Space size={8} wrap>
                    {cur.status ? (
                      <Tag>{EDUCATION_STATUS_LABEL[cur.status]}</Tag>
                    ) : (
                      <Select<EducationStatus>
                        size="small"
                        placeholder="选就读状态"
                        aria-label={`${cur.school}就读状态`}
                        disabled={busy}
                        onChange={(status) =>
                          update.mutate({ id, body: { status, expected_revision: revision } })
                        }
                        options={(['enrolled', 'completed'] as const).map((value) => ({
                          value,
                          label: EDUCATION_STATUS_LABEL[value],
                        }))}
                      />
                    )}
                    {d && d.draft_status !== 'pending' && (
                      <Tag color={d.draft_status === 'confirmed' ? 'success' : 'error'}>
                        {ITEM_STATUS[d.draft_status]}
                      </Tag>
                    )}
                    {cur.source === 'user' && <UserFilledTag label="你补充的" />}
                    {d?.review_reasons.map((reason) => (
                      <Tag key={reason} color="warning" icon={<WarningOutlined />}>
                        {REVIEW_REASON[reason] ?? reason}
                      </Tag>
                    ))}
                  </Space>
                  {cur.source_quote && cur.source_quote !== summary && (
                    <Typography.Text type="secondary" className="fact-quote">
                      原文：{cur.source_quote}
                    </Typography.Text>
                  )}
                </div>
              </HoverRow>
            )
          })}
        </ul>
      )}
      <Modal
        open={modalOpen}
        title={editingId ? '编辑教育经历' : '添加教育经历'}
        okText="保存"
        cancelText="取消"
        confirmLoading={add.isPending || update.isPending}
        okButtonProps={{ disabled: !form.school.trim() }}
        onOk={submitForm}
        onCancel={() => setModalOpen(false)}
      >
        <Space orientation="vertical" size={12} className="full-width">
          <Input
            placeholder="学校名称（必填）"
            aria-label="学校"
            value={form.school}
            onChange={(e) => setForm({ ...form, school: e.target.value })}
          />
          <Input
            placeholder="专业（可选）"
            aria-label="专业"
            value={form.major}
            onChange={(e) => setForm({ ...form, major: e.target.value })}
          />
          <Space size={8} wrap>
            <Select<DegreeLevel>
              value={form.degree}
              aria-label="学位"
              onChange={(degree) => setForm({ ...form, degree })}
              options={(['associate', 'bachelor', 'master', 'phd'] as const).map((value) => ({
                value,
                label: DEGREE_LABEL[value],
              }))}
            />
            <Select<EducationStatus>
              value={form.status}
              aria-label="就读状态"
              onChange={(status) => setForm({ ...form, status })}
              options={(['enrolled', 'completed'] as const).map((value) => ({
                value,
                label: EDUCATION_STATUS_LABEL[value],
              }))}
            />
          </Space>
          <Space size={8} wrap>
            <Input
              placeholder="开始时间，如 2023.09"
              aria-label="开始时间"
              value={form.start_text}
              onChange={(e) => setForm({ ...form, start_text: e.target.value })}
            />
            <Input
              placeholder="结束时间，如 2027.06（预计）"
              aria-label="结束时间"
              value={form.end_text}
              onChange={(e) => setForm({ ...form, end_text: e.target.value })}
            />
          </Space>
        </Space>
      </Modal>
    </Card>
  )
}
