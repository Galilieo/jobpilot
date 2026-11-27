// 页面渲染出错时只替换出错的那一块，侧栏和其他页面照常可用；不负责接口错误，接口错误由各页面自己提示。
import { Button, Result, Space } from 'antd'
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Link } from 'react-router'

interface PageErrorBoundaryProps {
  children: ReactNode
  // 变化时清掉错误，例如切换页面后不再显示上一页的出错提示。
  resetKey?: string
}

interface PageErrorBoundaryState {
  error: Error | null
  failedKey: string | undefined
}

export class PageErrorBoundary extends Component<PageErrorBoundaryProps, PageErrorBoundaryState> {
  state: PageErrorBoundaryState = { error: null, failedKey: undefined }

  static getDerivedStateFromError(error: Error): Partial<PageErrorBoundaryState> {
    return { error }
  }

  static getDerivedStateFromProps(
    props: PageErrorBoundaryProps,
    state: PageErrorBoundaryState,
  ): Partial<PageErrorBoundaryState> | null {
    if (state.error === null) return { failedKey: props.resetKey }
    if (props.resetKey !== state.failedKey) return { error: null, failedKey: props.resetKey }
    return null
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 开发时在控制台留下组件栈，方便定位是哪个字段或组件出的错。
    console.error('页面渲染出错', error, info.componentStack)
  }

  render(): ReactNode {
    if (this.state.error === null) return this.props.children
    return (
      <Result
        status="error"
        title="页面出错了"
        subTitle="页面遇到了意外错误，可以重试，或回到工作台。"
        extra={
          <Space>
            <Button type="primary" onClick={() => this.setState({ error: null })}>
              重试
            </Button>
            <Link to="/workspace">回到工作台</Link>
          </Space>
        }
      />
    )
  }
}
