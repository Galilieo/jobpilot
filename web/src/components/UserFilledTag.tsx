// 「你填写 / 你补充的」标签：默认 AI 提取内容不标，仅对用户改过或补充的内容标出。
import { Tag } from 'antd'
import type { ReactElement } from 'react'

export function UserFilledTag({ label = '你填写' }: { label?: string }): ReactElement {
  return <Tag color="processing">{label}</Tag>
}
