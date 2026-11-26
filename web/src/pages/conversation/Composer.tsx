// 对话输入框：Enter 发送，Shift + Enter 换行。文字由页面持有，消息没保存时页面把文字还回来。
import { Sender } from '@ant-design/x'
import { Typography } from 'antd'
import type { ReactElement } from 'react'

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  // 运行没结束或有卡等你决定时不能发新消息，placeholder 说明原因。
  blockedReason: string | null
}

export function Composer({
  value,
  onChange,
  onSubmit,
  blockedReason,
}: ComposerProps): ReactElement {
  const canSend = blockedReason === null && value.trim().length > 0
  return (
    <div className="conversation-composer">
      <Sender
        value={value}
        onChange={(next) => onChange(next)}
        onSubmit={() => canSend && onSubmit()}
        submitType="enter"
        placeholder={blockedReason ?? '发 JD、问匹配情况，或让我改一条简历…'}
        autoSize={{ minRows: 2, maxRows: 8 }}
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
