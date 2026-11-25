// 账号设置和待删除时的恢复入口，共用导出与结束会话的操作。
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Divider, Flex, Modal, Typography, theme } from 'antd'
import { useState, type ReactElement } from 'react'
import { useNavigate } from 'react-router'
import { account, auth } from '../api/endpoints'
import { isUnauthorized, ME_QUERY, useCurrentUser } from '../auth'
import { PageHeader } from '../components/PageHeader'

const DELETION_NOTICE = '申请后 7 天内可重新登录并恢复。到期后数据会彻底删除，无法找回。'

function useEndSession(action: () => Promise<void>) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  return useMutation({
    mutationFn: async () => {
      try {
        await action()
      } catch (error) {
        if (!isUnauthorized(error)) throw error
      }
    },
    onSuccess: () => {
      queryClient.clear()
      void navigate('/login', { replace: true })
    },
  })
}

function ExportButton(): ReactElement {
  return (
    <Button href={account.exportUrl} download>
      导出全部数据
    </Button>
  )
}

export function AccountPage(): ReactElement {
  const user = useCurrentUser()
  const logout = useEndSession(auth.logout)
  const deletion = useEndSession(account.requestDeletion)
  const [confirmOpen, setConfirmOpen] = useState(false)

  return (
    <section className="page">
      <PageHeader title="账号设置" />
      <Card>
        <Flex vertical align="start" gap={12}>
          <Typography.Title level={3}>登录信息</Typography.Title>
          <Typography.Text>{user.data?.email}</Typography.Text>
          <Button onClick={() => logout.mutate()} loading={logout.isPending}>
            退出登录
          </Button>
          {logout.error && <Alert type="error" showIcon title={logout.error.message} />}
        </Flex>
        <Divider />
        <Flex vertical align="start" gap={12}>
          <Typography.Title level={3}>个人数据</Typography.Title>
          <ExportButton />
        </Flex>
        <Divider />
        <Flex vertical align="start" gap={12}>
          <Typography.Title level={3}>删除账号</Typography.Title>
          <Typography.Paragraph className="account-notice">{DELETION_NOTICE}</Typography.Paragraph>
          <Button danger onClick={() => setConfirmOpen(true)} disabled={logout.isPending}>
            删除账号
          </Button>
        </Flex>
      </Card>
      <Modal
        title="确认删除账号？"
        open={confirmOpen}
        okText="确认删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        cancelButtonProps={{ disabled: deletion.isPending }}
        closable={!deletion.isPending}
        mask={{ closable: !deletion.isPending }}
        keyboard={!deletion.isPending}
        confirmLoading={deletion.isPending}
        onOk={() => deletion.mutate()}
        onCancel={() => {
          deletion.reset()
          setConfirmOpen(false)
        }}
      >
        <Typography.Paragraph>所有设备都会退出登录。{DELETION_NOTICE}</Typography.Paragraph>
        {deletion.error && <Alert type="error" showIcon title={deletion.error.message} />}
      </Modal>
    </section>
  )
}

export function AccountRecoveryPage({ deadline }: { deadline: string }): ReactElement {
  const { token } = theme.useToken()
  const queryClient = useQueryClient()
  const logout = useEndSession(auth.logout)
  const restore = useMutation({
    mutationFn: account.restore,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ME_QUERY }),
  })
  const deadlineLabel = new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'long',
    timeStyle: 'short',
  }).format(new Date(deadline))
  const error = restore.error ?? logout.error

  return (
    <main className="login-page" style={{ background: token.colorBgLayout }}>
      <Card className="login-card">
        <Flex vertical align="start" gap={16}>
          <Typography.Title level={1}>账号待删除</Typography.Title>
          <Typography.Text>
            恢复截止时间为 <time dateTime={deadline}>{deadlineLabel}</time>。
          </Typography.Text>
          {error && <Alert type="error" showIcon title={error.message} />}
          <Button
            type="primary"
            onClick={() => restore.mutate()}
            loading={restore.isPending}
            disabled={logout.isPending}
          >
            恢复账号
          </Button>
          <ExportButton />
          <Button
            onClick={() => logout.mutate()}
            loading={logout.isPending}
            disabled={restore.isPending}
          >
            退出登录
          </Button>
        </Flex>
      </Card>
    </main>
  )
}
