import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type ReactElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../test/render'
import { PageErrorBoundary } from './PageErrorBoundary'

const state = { broken: true }

function Fragile(): ReactElement {
  if (state.broken) throw new Error('缺少字段')
  return <p>页面内容</p>
}

describe('PageErrorBoundary', () => {
  afterEach(() => {
    state.broken = true
    vi.restoreAllMocks()
  })

  it('渲染出错时只显示提示，修好后点重试恢复', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()
    renderWithProviders(
      <div>
        <nav>侧栏</nav>
        <PageErrorBoundary resetKey="/applications">
          <Fragile />
        </PageErrorBoundary>
      </div>,
    )

    expect(screen.getByText('页面出错了')).toBeInTheDocument()
    expect(screen.getByText('侧栏')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '回到工作台' })).toHaveAttribute('href', '/workspace')

    state.broken = false
    await user.click(screen.getByRole('button', { name: /重\s*试/ }))
    expect(screen.getByText('页面内容')).toBeInTheDocument()
  })

  it('换了页面就清掉上一页的错误', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()

    // 在同一棵树里换 resetKey，才能测到错误状态有没有随页面清掉。
    function Shell(): ReactElement {
      const [path, setPath] = useState('/applications')
      return (
        <>
          <button
            type="button"
            onClick={() => {
              state.broken = false
              setPath('/jobs')
            }}
          >
            去岗位库
          </button>
          <PageErrorBoundary resetKey={path}>
            <Fragile />
          </PageErrorBoundary>
        </>
      )
    }

    renderWithProviders(<Shell />)
    expect(screen.getByText('页面出错了')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '去岗位库' }))
    expect(screen.getByText('页面内容')).toBeInTheDocument()
  })
})
