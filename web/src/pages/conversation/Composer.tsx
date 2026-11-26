// 对话输入框：Enter 发送，Shift + Enter 换行，可带文字附件。
// 文字和附件由页面持有，消息没保存时页面把它们还回来。
import { CloseOutlined, FileTextOutlined } from '@ant-design/icons'
import { Sender } from '@ant-design/x'
import { Typography } from 'antd'
import type { ReactElement } from 'react'
import type { AgentTextAttachment } from '../../api/types'
import { AttachmentPicker } from './AttachmentPicker'

export interface Draft {
  text: string
  attachments: AgentTextAttachment[]
}

interface ComposerProps {
  draft: Draft
  onChange: (draft: Draft) => void
  onSubmit: () => void
  // 运行没结束或有卡等你决定时不能发新消息，placeholder 说明原因。
  blockedReason: string | null
}

export function Composer({
  draft,
  onChange,
  onSubmit,
  blockedReason,
}: ComposerProps): ReactElement {
  const hasContent = draft.text.trim().length > 0 || draft.attachments.length > 0
  const canSend = blockedReason === null && hasContent
  const { attachments } = draft

  return (
    <div className="conversation-composer">
      <Sender
        value={draft.text}
        onChange={(text) => onChange({ ...draft, text })}
        onSubmit={() => canSend && onSubmit()}
        submitType="enter"
        placeholder={blockedReason ?? '发 JD、问匹配情况，或让我改一条简历…'}
        autoSize={{ minRows: 2, maxRows: 8 }}
        header={
          attachments.length > 0 && (
            <div className="composer-attachments">
              {attachments.map((item, index) => (
                <span key={index} className="attachment-chip">
                  <FileTextOutlined />
                  <span className="attachment-name">{item.name}</span>
                  <CloseOutlined
                    role="button"
                    aria-label={`移除 ${item.name}`}
                    onClick={() =>
                      onChange({ ...draft, attachments: attachments.filter((_, i) => i !== index) })
                    }
                  />
                </span>
              ))}
            </div>
          )
        }
        prefix={
          <AttachmentPicker
            count={attachments.length}
            onAdd={(item) => onChange({ ...draft, attachments: [...attachments, item] })}
          />
        }
        suffix={(_, { components: { SendButton } }) => (
          <SendButton type="primary" disabled={!canSend} aria-label="发送" />
        )}
      />
      <Typography.Text type="secondary" className="composer-note">
        新增、修改和删除前，会先请你确认。
      </Typography.Text>
    </div>
  )
}
