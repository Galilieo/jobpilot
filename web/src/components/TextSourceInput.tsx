// 通用文字来源输入：支持粘贴、上传文件、上传截图三种方式，负责把内容转成可编辑的纯文本。
// 不知道文字拿去干什么（简历拆分或 JD 解析），也不负责提交保存。
import { InboxOutlined } from '@ant-design/icons'
import { App, Input, Segmented, Typography, Upload } from 'antd'
import { useState, type ReactElement } from 'react'
import { textImport } from '../api/endpoints'

type Source = 'paste' | 'file' | 'screenshots'

const FILE_TYPES = '.txt,.md,.markdown,.html,.htm,.docx,.pdf'
// 与后端 parsing.MAX_OCR_IMAGES 保持一致，避免超限传完整批文件才报错。
const MAX_SCREENSHOTS = 10

export interface TextSourceCopy {
  filePrompt: string
  screenshotsPrompt: string
  pasteHint: string
  placeholder: string
}

interface TextSourceInputProps {
  value: string
  onChange: (value: string) => void
  copy: TextSourceCopy
  onFilenameChange?: (filename: string | null) => void
  onReadingChange?: (reading: boolean) => void
}

export function TextSourceInput({
  value,
  onChange,
  copy,
  onFilenameChange,
  onReadingChange,
}: TextSourceInputProps): ReactElement {
  const { message } = App.useApp()
  const [source, setSource] = useState<Source>('paste')
  const [reading, setReading] = useState(false)

  function updateReading(next: boolean): void {
    setReading(next)
    onReadingChange?.(next)
  }

  async function readFile(file: File): Promise<void> {
    updateReading(true)
    try {
      const { text } = await textImport.extract(file)
      onChange(text)
      onFilenameChange?.(file.name)
    } catch (error) {
      void message.error(error instanceof Error ? error.message : '没能读取这个文件')
    } finally {
      updateReading(false)
    }
  }

  async function readScreenshots(files: File[]): Promise<void> {
    if (files.length > MAX_SCREENSHOTS) {
      void message.error(`一次最多上传 ${MAX_SCREENSHOTS} 张截图`)
      return
    }
    updateReading(true)
    try {
      const { text } = await textImport.ocr(files)
      onChange(text)
      onFilenameChange?.(null)
    } catch (error) {
      void message.error(error instanceof Error ? error.message : '没能识别这些截图')
    } finally {
      updateReading(false)
    }
  }

  return (
    <>
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
          <p>{copy.filePrompt}</p>
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
          beforeUpload={(file, fileList) => {
            // 多选时每个文件都会调一次，只在最后一个时统一识别。
            if (file === fileList[fileList.length - 1]) void readScreenshots(fileList)
            return false
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p>{copy.screenshotsPrompt}</p>
          <Typography.Text type="secondary">
            最多 {MAX_SCREENSHOTS} 张，按上传顺序拼接，在本机识别文字
          </Typography.Text>
        </Upload.Dragger>
      )}
      <Typography.Paragraph type="secondary" className="import-hint">
        {source === 'paste'
          ? copy.pasteHint
          : '读取后的文字会显示在下面，提交前请核对：断行、错字要在这里改掉。'}
      </Typography.Paragraph>
      <Input.TextArea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoSize={{ minRows: 6, maxRows: 18 }}
        placeholder={reading ? '正在读取…' : copy.placeholder}
        disabled={reading}
      />
    </>
  )
}
