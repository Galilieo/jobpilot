// 登录与注册：同一个表单，切换模式。注册成功后直接登录。
import { useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, Segmented, Typography, theme } from 'antd'
import { useState, type ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { auth } from '../api/endpoints'
import { ME_QUERY } from '../auth'

type Mode = 'login' | 'register'

interface Credentials {
  email: string
  password: string
}

export function LoginPage(): ReactElement {
  const { token } = theme.useToken()
  const [mode, setMode] = useState<Mode>('login')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const destination = (location.state as { from?: string } | null)?.from ?? '/workspace'

  async function submit({ email, password }: Credentials): Promise<void> {
    setSubmitting(true)
    setError(null)
    try {
      if (mode === 'register') await auth.register(email, password)
      await auth.login(email, password)
      await queryClient.invalidateQueries({ queryKey: ME_QUERY })
      void navigate(destination, { replace: true })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '登录失败，请稍后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-page" style={{ background: token.colorBgLayout }}>
      <Card className="login-card">
        <div className="brand login-brand">
          Job<span style={{ color: token.colorPrimary }}>Pilot</span>
        </div>
        <Typography.Paragraph type="secondary">
          用你确认过的经历，为每个岗位生成不编造的定制简历。
        </Typography.Paragraph>
        <Segmented<Mode>
          block
          value={mode}
          onChange={(value) => {
            setMode(value)
            setError(null)
          }}
          options={[
            { label: '登录', value: 'login' },
            { label: '注册', value: 'register' },
          ]}
        />
        <Form<Credentials>
          layout="vertical"
          onFinish={submit}
          requiredMark={false}
          className="login-form"
        >
          <Form.Item
            label="邮箱"
            name="email"
            rules={[{ required: true, type: 'email', message: '请填写有效的邮箱' }]}
          >
            <Input autoComplete="email" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[
              { required: true, message: '请填写密码' },
              ...(mode === 'register' ? [{ min: 8, message: '密码至少 8 位' }] : []),
            ]}
          >
            <Input.Password autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
          </Form.Item>
          {error && <Alert type="error" showIcon title={error} className="login-error" />}
          <Button type="primary" htmlType="submit" block loading={submitting}>
            {mode === 'login' ? '登录' : '注册并登录'}
          </Button>
        </Form>
      </Card>
    </main>
  )
}
