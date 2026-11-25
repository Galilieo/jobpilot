// 导入简历：粘贴、上传文件、上传截图三种方式都先变成文字，用户核对修改后再提交拆分。
// 原文位置按提交的文字计算，所以提取错误（硬换行、双栏交错）要在这里改掉。
import { InboxOutlined } from '@ant-design/icons'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Input, Segmented, Typography, Upload } from 'antd'
import { useRef, useState, type ReactElement } from 'react'
import { resumes } from '../../api/endpoints'
import { newIdempotencyKey } from '../../api/http'
import { LIBRARY_QUERY } from '../../queryKeys'

type Source = 'paste' | 'file' | 'screenshots'

const FILE_TYPES = '.txt,.md,.markdown,.html,.htm,.docx,.pdf'

export function ResumeImport(): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [source, setSource] = useState<Source>('paste')
  const [text, setText] = useState('')
  const [filename, setFilename] = useState<string | null>(null)
  const [reading, setReading] = useState(false)
  // 同一次提交的重试用同一个键（服务端只拆分一次）；提交成功后换新键。
  const submissionKey = useRef(newIdempotencyKey())

  async function readFile(file: File): Promise<void> {
    setReading(true)
    try {
      const { text: extracted } = await resumes.extract(file)
      setText(extracted)
      setFilename(file.name)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '没能读取这个文件')
    } finally {
      setReading(false)
    }
  }

  async function readScreenshots(files: File[]): Promise<void> {
    setReading(true)
    try {
      const { text: recognized } = await resumes.ocr(files)
      setText(recognized)
      setFilename(null)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '没能识别这些截图')
    } finally {
      setReading(false)
    }
  }

  const submit = useMutation({
    mutationFn: () => resumes.upload(text, filename, submissionKey.current),
    onSuccess: async (uploaded) => {
      submissionKey.current = newIdempotencyKey()
      setText('')
      setFilename(null)
      await queryClient.invalidateQueries({ queryKey: LIBRARY_QUERY })
      message.success(`拆出 ${uploaded.items.length} 条经历事实，请在下方逐条核对`)
    },
    onError: (error) => {
      void message.error(error.message)
    },
  })

  return (
    <Card title="导入简历">
      <Segmented<Source>
        value={source}
        onChange={setSource}
        options={[
          { label: '粘贴文字', value: 'paste' },
          { label: '上传文件', value: 'file' },
          { label: '上传截图', value: 'screenshots' },
        ]}
      />
      {source === 'file' && (
        <Upload.Dragger
          className="import-upload"
          accept={FILE_TYPES}
          showUploadList={false}
          disabled={reading}
          beforeUpload={(file) => {
            void readFile(file)
            return false // 不让组件自己上传
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p>点击或拖入简历文件</p>
          <Typography.Text type="secondary">TXT、Markdown、HTML、DOCX、PDF</Typography.Text>
        </Upload.Dragger>
      )}
      {source === 'screenshots' && (
        <Upload.Dragger
          className="import-upload"
          accept="image/*"
          multiple
          showUploadList={false}
          disabled={reading}
          beforeUpload={(_file, fileList) => {
            // 多选时每个文件都会调一次，只在最后一个时统一识别。
            if (_file === fileList[fileList.length - 1]) void readScreenshots(fileList)
            return false
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p>点击或拖入简历截图，可以多张</p>
          <Typography.Text type="secondary">按上传顺序拼接，在本机识别文字</Typography.Text>
        </Upload.Dragger>
      )}
      <Typography.Paragraph type="secondary" className="import-hint">
        {source === 'paste'
          ? '把简历全文粘贴到下面。'
          : '读取后的文字会显示在下面，提交前请核对：断行、错字要在这里改掉。'}
      </Typography.Paragraph>
      <Input.TextArea
        value={text}
        onChange={(event) => setText(event.target.value)}
        autoSize={{ minRows: 6, maxRows: 18 }}
        placeholder={reading ? '正在读取…' : '简历文字'}
        disabled={reading}
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
