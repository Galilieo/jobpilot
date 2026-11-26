import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/http'
import type { ConversationDetail } from '../../api/types'
import {
  MAX_POLLS,
  POLL_MS,
  applyEvent,
  emptyTurn,
  pollDelay,
  withNetworkRetry,
} from './runControl'

function detail(changes: Partial<ConversationDetail>): ConversationDetail {
  return {
    conversation_id: 'c',
    title: 't',
    current_job_id: null,
    created_at: '',
    updated_at: '',
    messages: [],
    run: null,
    pending_action: null,
    latest_decided_action: null,
    confirmation_status: null,
    ...changes,
  }
}

const run = (status: NonNullable<ConversationDetail['run']>['status']) => ({
  run_id: 'r',
  status,
  stop_reason: null,
})

describe('轮询规则', () => {
  it('运行中、决定已保存、恢复中时轮询', () => {
    expect(pollDelay(detail({ run: run('running') }), 0)).toBe(POLL_MS)
    expect(
      pollDelay(
        detail({ run: run('waiting_confirmation'), confirmation_status: 'decision_saved' }),
        0,
      ),
    ).toBe(POLL_MS)
    expect(
      pollDelay(detail({ run: run('waiting_confirmation'), confirmation_status: 'recovering' }), 3),
    ).toBe(POLL_MS)
  })

  it('终态和等用户决定时停止，超过上限也停止', () => {
    expect(pollDelay(detail({ run: run('succeeded') }), 0)).toBe(false)
    expect(pollDelay(detail({ run: run('failed') }), 0)).toBe(false)
    expect(
      pollDelay(
        detail({ run: run('waiting_confirmation'), confirmation_status: 'waiting_decision' }),
        0,
      ),
    ).toBe(false)
    expect(pollDelay(detail({ run: null }), 0)).toBe(false)
    expect(pollDelay(detail({ run: run('running') }), MAX_POLLS)).toBe(false)
  })
})

describe('事件合并', () => {
  it('工具开始时清掉之前的文字，结束时按调用 ID 更新状态', () => {
    let turn = emptyTurn()
    turn = applyEvent(turn, { event: 'text', data: { text: '我先看看' } })
    turn = applyEvent(turn, { event: 'tool_start', data: { tool: 'list_jobs', tool_call_id: 'a' } })
    expect(turn.text).toBe('')
    turn = applyEvent(turn, {
      event: 'tool_end',
      data: { tool: 'list_jobs', tool_call_id: 'a', total: 2 },
    })
    turn = applyEvent(turn, {
      event: 'tool_end',
      data: { tool: 'read_job', tool_call_id: 'b', error: '岗位不存在' },
    })
    expect(turn.steps.map((step) => [step.id, step.status])).toEqual([
      ['a', 'success'],
      ['b', 'error'],
    ])
    turn = applyEvent(turn, { event: 'text', data: { text: '有两个' } })
    turn = applyEvent(turn, { event: 'text', data: { text: '岗位' } })
    expect(turn.text).toBe('有两个岗位')
  })
})

describe('网络重试', () => {
  it('拿不到响应时重试，其他错误不重试', async () => {
    const attempt = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, '网络', null))
      .mockResolvedValueOnce('ok')
    await expect(withNetworkRetry(attempt, { delayMs: 0 })).resolves.toBe('ok')
    expect(attempt).toHaveBeenCalledTimes(2)

    const conflict = vi.fn().mockRejectedValue(new ApiError(409, '冲突', null))
    await expect(withNetworkRetry(conflict, { delayMs: 0 })).rejects.toMatchObject({ status: 409 })
    expect(conflict).toHaveBeenCalledTimes(1)
  })
})
