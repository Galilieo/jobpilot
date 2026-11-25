import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from './http'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('request HTTP 错误整理与请求约定', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('401 与 404 返回中文提示', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(401, {}))
        .mockResolvedValueOnce(jsonResponse(404, {})),
    )

    await expect(request('/api/me')).rejects.toMatchObject({
      name: 'Error',
      status: 401,
      message: '登录已过期，请重新登录。',
    })
    await expect(request('/api/jobs/missing')).rejects.toMatchObject({
      status: 404,
      message: '没有找到这项内容，可能已被删除。',
    })
  })

  it('409 优先提取 detail.message，无 detail 时回退默认提示', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(
          jsonResponse(409, {
            detail: { message: '草稿已在别处更新，请刷新后再操作', revision: 4 },
          }),
        )
        .mockResolvedValueOnce(jsonResponse(409, null)),
    )

    await expect(request('/api/facts/1', { method: 'PUT', json: {} })).rejects.toMatchObject({
      status: 409,
      message: '草稿已在别处更新，请刷新后再操作',
      detail: { message: '草稿已在别处更新，请刷新后再操作', revision: 4 },
    })
    await expect(request('/api/facts/1', { method: 'PUT', json: {} })).rejects.toMatchObject({
      status: 409,
      message: '内容已在别处更新，请刷新后再试。',
    })
  })

  it('422 列表分别处理字符串列表与 FastAPI 校验对象列表', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(422, { detail: ['缺少经历名称', '文字太长'] }))
        .mockResolvedValueOnce(
          jsonResponse(422, { detail: [{ loc: ['body', 'text'], msg: 'Field required' }] }),
        ),
    )

    await expect(request('/api/facts', { json: {} })).rejects.toMatchObject({
      status: 422,
      message: '缺少经历名称；文字太长',
    })
    await expect(request('/api/facts', { json: {} })).rejects.toMatchObject({
      status: 422,
      message: '填写的内容不符合要求，请检查后再试。',
    })
  })

  it('断网抛出 status=0 的 ApiError，AbortError 原样抛出', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockRejectedValueOnce(new DOMException('Aborted', 'AbortError')),
    )

    const networkError = await request('/api/overview').catch((e: unknown) => e)
    expect(networkError).toBeInstanceOf(ApiError)
    expect(networkError).toMatchObject({
      status: 0,
      message: '网络连接失败，请检查网络后重试。',
    })

    const abortError = await request('/api/overview').catch((e: unknown) => e)
    expect(abortError).toBeInstanceOf(DOMException)
    expect((abortError as DOMException).name).toBe('AbortError')
  })

  it('204 正常返回且带上 Idempotency-Key 与 same-origin 凭证', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await request<void>('/api/facts/1', {
      method: 'DELETE',
      idempotencyKey: 'idem-key-123',
    })

    expect(result).toBeUndefined()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/facts/1',
      expect.objectContaining({
        method: 'DELETE',
        credentials: 'same-origin',
        headers: expect.objectContaining({ 'Idempotency-Key': 'idem-key-123' }),
      }),
    )
  })
})
