// 给消息添加文字附件：复用 TextSourceInput（粘贴、文件、截图都先转成文字，让用户核对），
// 加进来的只是文字，随消息一起发出。
import { PaperClipOutlined } from '@ant-design/icons'
import { Button, Modal, Tooltip } from 'antd'
import { useState, type ReactElement } from 'react'
import type { AgentTextAttachment } from '../../api/types'
import { TextSourceInput } from '../../components/TextSourceInput'

// 与后端 MessageIn.attachments 的 max_length 一致。
export const MAX_ATTACHMENTS = 5

const COPY = {
  filePrompt: '拖入或点击上传 JD、简历等文件',
  screenshotsPrompt: '拖入或点击上传截图',
  pasteHint: '粘贴 JD、简历或其他材料的文字。',
  placeholder: '附件文字',
}

interface AttachmentPickerProps {
  count: number
  onAdd: (attachment: AgentTextAttachment) => void
}

export function AttachmentPicker({ count, onAdd }: AttachmentPickerProps): ReactElement {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [name, setName] = useState<string | null>(null)
  const [reading, setReading] = useState(false)
  const full = count >= MAX_ATTACHMENTS

  function close(): void {
    setOpen(false)
    setText('')
    setName(null)
  }

  return (
    <>
      <Tooltip title={full ? `最多 ${MAX_ATTACHMENTS} 个附件` : '添加附件'}>
        <Button
          type="text"
          icon={<PaperClipOutlined />}
          aria-label="添加附件"
          disabled={full}
          onClick={() => setOpen(true)}
        />
      </Tooltip>
      <Modal
        title="添加附件"
        open={open}
        okText="加入附件"
        cancelText="取消"
        okButtonProps={{ disabled: !text.trim() || reading }}
        onOk={() => {
          onAdd({ kind: 'text', name: name ?? `附件 ${count + 1}`, text: text.trim() })
          close()
        }}
        onCancel={close}
        destroyOnHidden
      >
        <div className="attachment-picker">
          <TextSourceInput
            value={text}
            onChange={setText}
            copy={COPY}
            onFilenameChange={setName}
            onReadingChange={setReading}
          />
        </div>
      </Modal>
    </>
  )
}
