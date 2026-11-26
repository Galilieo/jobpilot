import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { jsonResponse, stubFetch } from '../test/fetchMock'
import { renderWithProviders } from '../test/render'
import { SideNavigation } from './SideNavigation'

describe('SideNavigation 头像菜单交互', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('展开时切换 aria-expanded 并带旋转箭头类，Esc 或点击外面关闭', async () => {
    const user = userEvent.setup()
    stubFetch({
      'GET /api/me': () =>
        jsonResponse(200, {
          user_id: 'u1',
          email: 'demo@jobpilot.test',
          deletion_requested_at: null,
          deletion_deadline: null,
        }),
      'GET /api/conversations': () => jsonResponse(200, []),
    })

    renderWithProviders(
      <div>
        <SideNavigation />
        <button type="button">外部区域</button>
      </div>,
    )

    const trigger = await screen.findByRole('button', { name: '打开账号菜单' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(trigger.querySelector('.account-chevron')).toBeInTheDocument()

    await user.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByRole('menuitem', { name: /账号设置/ })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => expect(trigger).toHaveAttribute('aria-expanded', 'false'))
    expect(trigger).toHaveFocus()

    await user.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await user.click(screen.getByRole('button', { name: '外部区域' }))
    await waitFor(() => expect(trigger).toHaveAttribute('aria-expanded', 'false'))
  })
})
