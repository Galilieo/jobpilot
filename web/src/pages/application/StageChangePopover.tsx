// 看板卡片、列表下拉和详情抽屉共用的改阶段弹层：选目标阶段（或固定目标阶段）与发生日期。
import { useMutation } from '@tanstack/react-query'
import { App, Button, Input, Popover, Select } from 'antd'
import { useState, type ReactElement, type ReactNode } from 'react'
import { applications } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import type { ApplicationRecord, ApplicationStage } from '../../api/types'
import { APPLICATION_STAGE } from '../../labels'
import { BOARD_STAGES, suggestedNextStage, todayLocalDate } from './applicationStatus'

interface StageChangePopoverProps {
  record: ApplicationRecord
  refresh: () => Promise<void>
  fixedStage?: ApplicationStage
  open?: boolean
  onOpenChange?: (open: boolean) => void
  onCancel?: () => void
  triggerLabel?: string
  children?: ReactNode
}

function StageChangeForm({
  record,
  refresh,
  fixedStage,
  onClose,
  onCancel,
}: {
  record: ApplicationRecord
  refresh: () => Promise<void>
  fixedStage?: ApplicationStage
  onClose: () => void
  onCancel?: () => void
}): ReactElement {
  const { message } = App.useApp()
  const today = todayLocalDate()
  // 五个阶段都列出，当前阶段标出来但不能选，免得看起来像少了一项。
  const stageOptions = BOARD_STAGES.map((stage) => ({
    value: stage,
    label:
      stage === record.stage ? `${APPLICATION_STAGE[stage]}（当前）` : APPLICATION_STAGE[stage],
    disabled: stage === record.stage,
  }))

  const [selectedStage, setSelectedStage] = useState<ApplicationStage>(() =>
    suggestedNextStage(record.stage),
  )
  const [occurredAt, setOccurredAt] = useState(today)
  const targetStage = fixedStage ?? selectedStage

  const updateStage = useMutation({
    mutationFn: () =>
      applications.update(record.application_id, {
        expected_revision: record.revision,
        stage: targetStage,
        occurred_at: occurredAt,
      }),
    onSuccess: async () => {
      onClose()
      await refresh()
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) {
        onClose()
        await refresh()
        void message.warning('投递记录刚被别处改过，已重新读取')
        return
      }
      void message.error(error.message)
    },
  })

  function submit(): void {
    const maxDate = todayLocalDate()
    if (!occurredAt || occurredAt > maxDate) {
      void message.warning('发生日期不能晚于今天')
      return
    }
    updateStage.mutate()
  }

  const jobTitle = record.job_snapshot_json.title ?? '未命名岗位'

  return (
    <div
      className="stage-pop-form"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <div className="stage-pop-title">
        {fixedStage ? `改到「${APPLICATION_STAGE[fixedStage]}」` : `把「${jobTitle}」改到`}
      </div>
      {!fixedStage && (
        <label className="stage-pop-field">
          <span>阶段</span>
          <Select<ApplicationStage>
            aria-label="目标阶段"
            size="small"
            value={selectedStage}
            options={stageOptions}
            onChange={setSelectedStage}
          />
        </label>
      )}
      <label className="stage-pop-field">
        <span>发生日期</span>
        <Input
          type="date"
          size="small"
          aria-label="发生日期"
          value={occurredAt}
          max={today}
          onChange={(event) => setOccurredAt(event.target.value)}
        />
      </label>
      <div className="stage-pop-actions">
        <Button
          size="small"
          onClick={() => {
            onClose()
            onCancel?.()
          }}
          disabled={updateStage.isPending}
        >
          取消
        </Button>
        <Button size="small" type="primary" loading={updateStage.isPending} onClick={submit}>
          改阶段
        </Button>
      </div>
    </div>
  )
}

export function StageChangePopover({
  record,
  refresh,
  fixedStage,
  open: controlledOpen,
  onOpenChange,
  onCancel,
  triggerLabel = '改阶段 ▾',
  children,
}: StageChangePopoverProps): ReactElement {
  const [internalOpen, setInternalOpen] = useState(false)
  const isControlled = controlledOpen !== undefined
  const open = isControlled ? controlledOpen : internalOpen

  function closeOnly(): void {
    if (!isControlled) setInternalOpen(false)
    onOpenChange?.(false)
  }

  return (
    <Popover
      open={open}
      trigger={isControlled ? [] : 'click'}
      placement="bottomRight"
      content={
        open ? (
          <StageChangeForm
            record={record}
            refresh={refresh}
            fixedStage={fixedStage}
            onClose={closeOnly}
            onCancel={onCancel}
          />
        ) : null
      }
      destroyOnHidden
      onOpenChange={(next) => {
        if (!next) {
          closeOnly()
          onCancel?.()
        } else if (!isControlled) {
          setInternalOpen(true)
          onOpenChange?.(true)
        }
      }}
    >
      {children ?? (
        <Button
          size="small"
          type="text"
          className="app-move-btn"
          onClick={(event) => {
            event.stopPropagation()
            const next = !open
            if (!isControlled) setInternalOpen(next)
            onOpenChange?.(next)
          }}
          onKeyDown={(event) => event.stopPropagation()}
        >
          {triggerLabel}
        </Button>
      )}
    </Popover>
  )
}
