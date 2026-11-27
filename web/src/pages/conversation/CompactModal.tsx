// 触发对话上下文压缩的小弹窗：可选填写重点（≤300 字），按状态分别提示 202 / not_needed / 409。
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { App, Input, Modal, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import { conversations } from '../../api/endpoints'
import { ApiError } from '../../api/http'
import { conversationQuery } from '../../queryKeys'

interface CompactModalProps {
  conversationId: string
  open: boolean
  onClose: () => void
}

export function CompactModal({ conversationId, open, onClose }: CompactModalProps): ReactElement {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [focus, setFocus] = useState('')

  const compact = useMutation({
    mutationFn: (trimmedFocus: string | undefined) =>
      conversations.compact(conversationId, trimmedFocus),
    onSuccess: (result) => {
      // 详情和用量都在这个前缀下，压缩开始后重新读取。
      void queryClient.invalidateQueries({ queryKey: conversationQuery(conversationId) })
      if (result.status === 'not_needed') {
        void message.info('对话还不长，不需要压缩')
      } else if (result.focus_applied) {
        void message.success('正在压缩较早的对话')
      } else {
        void message.info('正在压缩较早的对话；已经有一次压缩在进行，这次的重点没有用上')
      }
      setFocus('')
      onClose()
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void message.warning('对话正在进行，等这一轮结束再压缩')
        return
      }
      void message.error(error instanceof Error ? error.message : '压缩请求失败，请稍后重试')
    },
  })

  return (
    <Modal
      title="压缩上下文"
      open={open}
      okText="开始压缩"
      cancelText="取消"
      confirmLoading={compact.isPending}
      onOk={() => compact.mutate(focus.trim() || undefined)}
      onCancel={() => {
        if (!compact.isPending) {
          setFocus('')
          onClose()
        }
      }}
      destroyOnHidden
    >
      <Typography.Paragraph type="secondary">
        把较早的对话收拢成摘要，给后续交流腾出空间；最近几轮与重要决定都会保留。
      </Typography.Paragraph>
      <Input.TextArea
        value={focus}
        maxLength={300}
        showCount
        autoSize={{ minRows: 2, maxRows: 4 }}
        placeholder="想优先保留哪些内容？（选填，最多 300 字）"
        onChange={(event) => setFocus(event.target.value)}
      />
    </Modal>
  )
}
