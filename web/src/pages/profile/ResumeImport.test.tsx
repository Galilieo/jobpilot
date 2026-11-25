import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { ResumeImport } from './ResumeImport'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function idempotencyKeyOf(call: unknown[]): string {
  const init = call[1] as RequestInit
  return (init.headers as Record<string, string>)['Idempotency-Key']
}

describe('ResumeImport 幂等重试与输入保留', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('失败时保留输入并在同一次重试复用幂等键，成功后清空并换新键', async () => {
    const user = userEvent.setup()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(502, { detail: '事实拆分失败，请稍后重试' }))
      .mockResolvedValueOnce(
        jsonResponse(201, {
          doc_id: 'doc-1',
          run_id: 'run-1',
          items: [{ lineage_id: 'lin-1' }],
          revision: 1,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, {
          doc_id: 'doc-2',
          run_id: 'run-2',
          items: [{ lineage_id: 'lin-2' }],
          revision: 2,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<ResumeImport />)

    const textarea = screen.getByPlaceholderText('简历文字')
    const submitButton = screen.getByRole('button', { name: '提交并拆分经历' })

    // 第一次提交失败
    await user.type(textarea, '第一份合成简历正文')
    await user.click(submitButton)

    expect(await screen.findByText('事实拆分失败，请稍后重试')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText('正在拆分，通常需要半分钟左右')).not.toBeInTheDocument(),
    )
    expect(textarea).toHaveValue('第一份合成简历正文')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const firstKey = idempotencyKeyOf(fetchMock.mock.calls[0])
    expect(firstKey).toBeTruthy()

    // 同一次操作重试：必须复用同一个幂等键，成功后清空输入
    await user.click(submitButton)
    expect(await screen.findByText('拆出 1 条经历事实，请在下方逐条核对')).toBeInTheDocument()
    await waitFor(() => expect(textarea).toHaveValue(''))
    const retryKey = idempotencyKeyOf(fetchMock.mock.calls[1])
    expect(retryKey).toBe(firstKey)

    // 下一次新的提交：必须更换新的幂等键
    await user.type(textarea, '第二份合成简历正文')
    await user.click(submitButton)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    const nextKey = idempotencyKeyOf(fetchMock.mock.calls[2])
    expect(nextKey).toBeTruthy()
    expect(nextKey).not.toBe(firstKey)
  })
})
