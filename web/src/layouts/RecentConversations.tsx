// 侧栏的最近对话：列出、切换、重命名和删除。岗位对话和普通对话用不同图标。
// 不负责新建对话：新对话在用户发出第一条消息时由对话页创建。
import { DeleteOutlined, EditOutlined, MessageOutlined, SolutionOutlined } from '@ant-design/icons'
import { Conversations } from '@ant-design/x'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Input, Modal, Skeleton, Typography } from 'antd'
import { useState, type ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { conversations } from '../api/endpoints'
import type { Conversation } from '../api/types'
import { CONVERSATIONS_QUERY, conversationQuery } from '../queryKeys'

const CONVERSATION_PATH = /^\/conversations\/([^/]+)$/

export function RecentConversations({ onNavigate }: { onNavigate?: () => void }): ReactElement {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const activeId = CONVERSATION_PATH.exec(pathname)?.[1]
  const [renaming, setRenaming] = useState<Conversation | null>(null)
  const [title, setTitle] = useState('')

  const list = useQuery({
    queryKey: CONVERSATIONS_QUERY,
    queryFn: ({ signal }) => conversations.list(signal),
  })

  const rename = useMutation({
    mutationFn: ({ id, value }: { id: string; value: string }) =>
      conversations.update(id, { title: value }),
    onSuccess: async (saved) => {
      setRenaming(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY }),
        queryClient.invalidateQueries({ queryKey: conversationQuery(saved.conversation_id) }),
      ])
    },
    onError: (error) => void message.error(error.message),
  })

  const remove = useMutation({
    mutationFn: (id: string) => conversations.remove(id),
    onSuccess: async (_, id) => {
      queryClient.removeQueries({ queryKey: conversationQuery(id) })
      await queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY })
      message.success('已删除对话')
      if (id === activeId) void navigate('/conversations/new', { replace: true })
    },
    // 运行中的对话后端返回 409「对话正在运行，请稍后删除」，原样告诉用户。
    onError: (error) => void message.error(error.message),
  })

  function submitRename(): void {
    if (renaming && title.trim())
      rename.mutate({ id: renaming.conversation_id, value: title.trim() })
  }

  function confirmDelete(item: Conversation): void {
    modal.confirm({
      title: '删除这段对话？',
      content: '对话里的消息会一起删除，岗位库和定制简历不受影响。',
      okText: '删除对话',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => remove.mutateAsync(item.conversation_id).catch(() => undefined),
    })
  }

  if (list.isPending) return <Skeleton active paragraph={{ rows: 3 }} title={false} />
  if (list.isError) {
    return (
      <Typography.Text type="secondary" className="recent-empty">
        对话列表没加载出来，
        <Typography.Link onClick={() => void list.refetch()}>重试</Typography.Link>
      </Typography.Text>
    )
  }
  if (list.data.length === 0) {
    return (
      <Typography.Text type="secondary" className="recent-empty">
        还没有对话
      </Typography.Text>
    )
  }

  const byId = new Map(list.data.map((item) => [item.conversation_id, item]))
  return (
    <>
      <Conversations
        className="recent-list"
        activeKey={activeId}
        items={list.data.map((item) => ({
          key: item.conversation_id,
          label: item.title,
          icon: item.current_job_id ? (
            <SolutionOutlined aria-label="岗位对话" />
          ) : (
            <MessageOutlined aria-label="普通对话" />
          ),
        }))}
        onActiveChange={(key) => {
          void navigate(`/conversations/${key}`)
          onNavigate?.()
        }}
        menu={(info) => ({
          items: [
            { key: 'rename', icon: <EditOutlined />, label: '重命名' },
            { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
          ],
          onClick: ({ key, domEvent }) => {
            domEvent.stopPropagation()
            const item = byId.get(info.key)
            if (!item) return
            if (key === 'delete') confirmDelete(item)
            else {
              setTitle(item.title)
              setRenaming(item)
            }
          },
        })}
      />
      <Modal
        title="重命名对话"
        open={renaming !== null}
        okText="保存名称"
        cancelText="取消"
        okButtonProps={{ disabled: !title.trim(), loading: rename.isPending }}
        onOk={submitRename}
        onCancel={() => setRenaming(null)}
        destroyOnHidden
      >
        <Input
          aria-label="对话名称"
          value={title}
          maxLength={200}
          onChange={(event) => setTitle(event.target.value)}
          onPressEnter={submitRename}
        />
      </Modal>
    </>
  )
}
