import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { ContextMeterView } from './ContextMeter'
import { formatTokens } from './naming'

describe('上下文用量圆环', () => {
  it('按已用比例标注，悬停说明何时自动压缩，点击打开手动压缩', async () => {
    const user = userEvent.setup()
    const onCompact = vi.fn()
    renderWithProviders(
      <ContextMeterView
        usage={{ used_tokens: 25_600, window_tokens: 128_000, compact_at_tokens: 94_000 }}
        onCompact={onCompact}
      />,
    )

    const meter = screen.getByRole('button', { name: '上下文已用 20%，点击压缩' })
    expect(meter).not.toHaveClass('is-near')
    await user.hover(meter)
    expect(await screen.findByText('约 2.6 万 / 12.8 万 token')).toBeInTheDocument()
    expect(screen.getByText('到 73% 时自动压缩较早的对话')).toBeInTheDocument()
    await user.click(meter)
    expect(onCompact).toHaveBeenCalledTimes(1)
  })

  it('超过自动压缩线时变成提醒色并说明本轮后会压缩', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ContextMeterView
        usage={{ used_tokens: 100_000, window_tokens: 128_000, compact_at_tokens: 94_000 }}
        onCompact={vi.fn()}
      />,
    )
    const meter = screen.getByRole('button', { name: '上下文已用 78%，点击压缩' })
    expect(meter).toHaveClass('is-near')
    await user.hover(meter)
    expect(await screen.findByText('这一轮结束后会自动压缩较早的对话')).toBeInTheDocument()
  })

  it('一万以下按整数显示', () => {
    expect(formatTokens(9_999)).toBe('9999')
    expect(formatTokens(512_000)).toBe('51.2 万')
  })
})
