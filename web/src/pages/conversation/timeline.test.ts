import { describe, expect, it } from 'vitest'
import type { ConversationDetail, ConversationMessage } from '../../api/types'
import { buildTimeline } from './timeline'

function message(
  id: string,
  role: ConversationMessage['role'],
  steps: ConversationMessage['steps_json'] = [],
  content = '',
): ConversationMessage {
  return {
    message_id: id,
    seq: Number(id.slice(1)),
    run_id: 'r1',
    client_message_id: null,
    role,
    content,
    attachments_json: [],
    steps_json: steps,
    created_at: '',
  }
}

function detailOf(messages: ConversationMessage[]): ConversationDetail {
  return {
    conversation_id: 'c',
    title: 't',
    current_job_id: null,
    created_at: '',
    updated_at: '',
    messages,
    run: null,
    pending_action: null,
    latest_decided_action: null,
    confirmation_status: null,
  }
}

const titles: Record<string, string> = { j1: '后端实习', j2: '算法实习' }

describe('对话时间线', () => {
  it('第一次解析 JD 时提示已设为当前岗位，之后的 JD 只提示存入岗位库', () => {
    const items = buildTimeline(
      detailOf([
        message('m1', 'user', [], '这是 JD'),
        message('m2', 'tool', [{ tool: 'parse_jd', job_id: 'j1' }]),
        message('m3', 'tool', [{ tool: 'match_job', report_id: 'x' }]),
        message('m4', 'assistant', [], '匹配好了'),
        message('m5', 'tool', [{ tool: 'parse_jd', job_id: 'j2' }]),
      ]),
      (id) => titles[id],
    )
    expect(items.map((item) => item.type)).toEqual([
      'user',
      'steps',
      'hint',
      'assistant',
      'steps',
      'hint',
    ])
    expect(items.filter((item) => item.type === 'hint').map((item) => item.text)).toEqual([
      '已存入岗位库，并设为当前岗位：后端实习',
      '已存入岗位库：算法实习',
    ])
  })

  it('卡片单独成项，并带上卡后同名工具的结果', () => {
    const card = {
      action_id: 'a1',
      run_id: 'r1',
      tool: 'set_current_job',
      status: 'pending' as const,
      kind: 'confirmation' as const,
      args: { job_id: null },
    }
    const items = buildTimeline(
      detailOf([
        message('m1', 'tool', [{ confirm: card }]),
        message('m2', 'tool', [{ tool: 'set_current_job', job_id: null }]),
      ]),
      (id) => titles[id],
    )
    expect(items[0]).toMatchObject({ type: 'card', outcome: { tool: 'set_current_job' } })
    expect(items.at(-1)).toMatchObject({ type: 'hint', text: '已解除当前岗位' })
  })

  it('同一轮工具步骤中间夹着空助手消息时合并成一组，不同轮次才分开', () => {
    const m1 = message('m1', 'tool', [{ tool: 'parse_jd', job_id: 'j1' }])
    const m2 = message('m2', 'assistant', [], '   ')
    const m3 = message('m3', 'tool', [{ tool: 'match_job', report_id: 'r1' }])
    const m4 = {
      ...message('m4', 'tool', [{ tool: 'tailor_resume', run_id: 'tr1' }]),
      run_id: 'r2',
    }

    const items = buildTimeline(detailOf([m1, m2, m3, m4]), (id) => titles[id])
    const stepGroups = items.filter((item) => item.type === 'steps')
    expect(stepGroups).toHaveLength(2)
    expect(stepGroups[0].steps.map((s) => s.tool)).toEqual(['parse_jd', 'match_job'])
    expect(stepGroups[1].steps.map((s) => s.tool)).toEqual(['tailor_resume'])
  })
})
