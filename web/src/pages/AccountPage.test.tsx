import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppShell } from '../layouts/AppShell'
import { renderWithProviders } from '../test/render'
import { AccountPage } from './AccountPage'

const deletionRequestedAt = '2026-11-25T02:30:00Z'
const deletionDeadline = '2026-12-02T02:30:00Z'
const email = 'synthetic@account.invalid'

function stubAccount(initiallyPending: boolean) {
  let pending = initiallyPending
  const fetchAccount = vi.fn(async (path: string, options: RequestInit) => {
    if (path === '/api/me') {
      return new Response(
        JSON.stringify({
          user_id: 'owner',
          email,
          deletion_requested_at: pending ? deletionRequestedAt : null,
          deletion_deadline: pending ? deletionDeadline : null,
        }),
      )
    }
    if (path === '/api/account/restore' && options.method === 'POST') {
      pending = false
      return new Response(null, { status: 204 })
    }
    if (path === '/api/account' && options.method === 'DELETE') {
      return new Response(null, { status: 204 })
    }
    throw new Error(`未配置此请求 ${path}`)
  })
  vi.stubGlobal('fetch', fetchAccount)
  return fetchAccount
}

function renderAccountRoute(): void {
  renderWithProviders(
    <Routes>
      <Route path="/login" element={<h1>登录页</h1>} />
      <Route element={<AppShell />}>
        <Route path="/account" element={<AccountPage />} />
      </Route>
    </Routes>,
    { initialEntries: ['/account'] },
  )
}

describe('账号设置与恢复', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('取消确认会保留设置页，确认时才申请删除并回到登录页', async () => {
    const fetchAccount = stubAccount(false)
    const user = userEvent.setup()
    renderAccountRoute()

    expect(await screen.findByRole('heading', { name: '账号设置' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '导出全部数据' })).toHaveAttribute(
      'href',
      '/api/account/export',
    )
    const requestDeletion = screen.getByRole('button', { name: '删除账号' })
    await user.click(requestDeletion)
    const dialog = screen.getByRole('dialog', { name: '确认删除账号？' })
    expect(within(dialog).getByText(/7 天内可重新登录并恢复/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /取\s*消/ }))
    expect(fetchAccount.mock.calls.filter(([path]) => path === '/api/account')).toHaveLength(0)

    await user.click(requestDeletion)
    await user.click(screen.getByRole('button', { name: '确认删除' }))
    expect(await screen.findByRole('heading', { name: '登录页' })).toBeInTheDocument()
    expect(fetchAccount).toHaveBeenCalledWith(
      '/api/account',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('待删除时拦住业务页面，展示截止时间和导出入口，恢复后放行', async () => {
    const fetchAccount = stubAccount(true)
    const user = userEvent.setup()
    renderAccountRoute()

    expect(await screen.findByRole('heading', { name: '账号待删除' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '账号设置' })).toBeNull()
    expect(screen.getByRole('time')).toHaveAttribute('datetime', deletionDeadline)
    expect(screen.getByRole('link', { name: '导出全部数据' })).toHaveAttribute(
      'href',
      '/api/account/export',
    )
    await user.click(screen.getByRole('button', { name: '恢复账号' }))

    expect(await screen.findByRole('heading', { name: '账号设置' })).toBeInTheDocument()
    expect(fetchAccount).toHaveBeenCalledWith(
      '/api/account/restore',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
