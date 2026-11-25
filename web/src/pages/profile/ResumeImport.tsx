// 导入简历：粘贴、上传文件、上传截图三种方式都先变成文字，用户核对修改后再提交拆分。
// 0 条事实时内联展示（ResumeImport），已有事实时通过弹窗打开（ImportResumeModal）。
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Modal, Typography } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { resumes } from '../../api/endpoints'
import { newIdempotencyKey } from '../../api/http'
import { TextSourceInput } from '../../components/TextSourceInput'
import { LIBRARY_QUERY } from '../../queryKeys'

const RESUME_COPY = {
  filePrompt: '点击或拖入简历文件',
  screenshotsPrompt: '点击或拖入简历截图，可以多张',
  pasteHint: '把简历全文粘贴到下面。',
  placeholder: '简历文字',
}

function useResumeUpload(onDone?: () => void) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const [filename, setFilename] = useState<string | null>(null)
  const [reading, setReading] = useState(false)
  // 同一次提交的重试用同一个键（服务端只拆分一次）；提交成功后换新键。
  const submissionKey = useRef(newIdempotencyKey())

  const submit = useMutation({
    mutationFn: () => resumes.upload(text, filename, submissionKey.current),
    onSuccess: async (uploaded) => {
      submissionKey.current = newIdempotencyKey()
      setText('')
      setFilename(null)
      onDone?.()
      await queryClient.invalidateQueries({ queryKey: LIBRARY_QUERY })
      message.success(`拆出 ${uploaded.items.length} 条经历事实，请在下方逐条核对`)
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  return { text, setText, setFilename, reading, setReading, submit }
}

export function ResumeImport(): ReactElement {
  const { text, setText, setFilename, reading, setReading, submit } = useResumeUpload()

  return (
    <Card title="导入简历">
      <TextSourceInput
        value={text}
        onChange={setText}
        copy={RESUME_COPY}
        onFilenameChange={setFilename}
        onReadingChange={setReading}
      />
      <div className="import-actions">
        <Button
          type="primary"
          disabled={!text.trim() || reading}
          loading={submit.isPending}
          onClick={() => submit.mutate()}
        >
          提交并拆分经历
        </Button>
        {submit.isPending && (
          <Typography.Text type="secondary">正在拆分，通常需要半分钟左右</Typography.Text>
        )}
      </div>
    </Card>
  )
}

export function ImportResumeModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}): ReactElement {
  const { text, setText, setFilename, reading, setReading, submit } = useResumeUpload(onClose)
  const [segmentedKey, setSegmentedKey] = useState(0)

  return (
    <Modal
      title="导入简历"
      open={open}
      onCancel={onClose}
      afterOpenChange={(visible) => {
        if (visible) setSegmentedKey((prev) => prev + 1)
      }}
      width={720}
      okText="提交并拆分经历"
      cancelText="取消"
      okButtonProps={{ disabled: !text.trim() || reading }}
      confirmLoading={submit.isPending}
      onOk={() => submit.mutate()}
      styles={{ body: { maxHeight: '65vh', overflowY: 'auto' } }}
      destroyOnHidden
    >
      <TextSourceInput
        value={text}
        onChange={setText}
        copy={RESUME_COPY}
        maxRows={10}
        segmentedKey={segmentedKey}
        onFilenameChange={setFilename}
        onReadingChange={setReading}
      />
      {submit.isPending && (
        <Typography.Text type="secondary" className="import-hint">
          正在拆分，通常需要半分钟左右
        </Typography.Text>
      )}
    </Modal>
  )
}
