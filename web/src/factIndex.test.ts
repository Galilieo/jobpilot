import { describe, expect, it } from 'vitest'
import type { TailoredResumeData } from './api/types'
import { indexResumeFacts } from './factIndex'

function makeResume(): TailoredResumeData {
  return {
    resume_id: 'res-1',
    run_id: 'run-1',
    version_id: 'ver-1',
    version: 1,
    current_version_id: 'ver-1',
    versions: [{ version_id: 'ver-1', version: 1, created_at: '2026-11-26T00:00:00Z' }],
    content: {
      header: ['林同学'],
      style: { font: 'default', size_pt: 10.5, leading: 1.5 },
      sections: [
        {
          title: '项目经历',
          kind: 'project',
          entries: [
            {
              entry_id: 'e1',
              experience: '问答原型',
              heading: '问答原型',
              title: '问答原型',
              time: null,
              lines: [],
              bullets: [
                {
                  bullet_id: 'b1',
                  text: '用 FastAPI 实现问答接口',
                  fact_ids: ['f-b', 'f-a', 'f-b'],
                  origin: 'passed',
                  edited: false,
                  check: { text: '用 FastAPI 实现问答接口', layers: 'full', issues: [] },
                },
                {
                  bullet_id: 'b2',
                  text: '用 48 条回归样例评测混合检索',
                  fact_ids: ['f-c', 'f-a', 'f-missing'],
                  origin: 'passed',
                  edited: true,
                  check: { text: '用 48 条回归样例评测混合检索', layers: 'full', issues: [] },
                },
              ],
            },
          ],
        },
      ],
    },
    facts: {
      'f-a': {
        experience: '问答原型',
        text: '使用 PostgreSQL 和 pgvector',
        source: 'resume',
        source_quote: null,
      },
      'f-b': {
        experience: '问答原型',
        text: '用 FastAPI 实现问答接口',
        source: 'resume',
        source_quote: null,
      },
      'f-c': {
        experience: '问答原型',
        text: '整理 48 条评测集',
        source: 'user',
        source_quote: null,
      },
    },
  }
}

describe('indexResumeFacts 事实编号规则', () => {
  it('按首次引用顺序编为 F1、F2…，同一事实复用同一编号并去重', () => {
    const first = indexResumeFacts(makeResume())
    const second = indexResumeFacts(makeResume())

    expect(first.ordered.map((item) => [item.code, item.factId])).toEqual([
      ['F1', 'f-b'],
      ['F2', 'f-a'],
      ['F3', 'f-c'],
    ])
    expect(first.ordered).toEqual(second.ordered)
    expect(first.bulletFacts('b1').map((item) => item.code)).toEqual(['F1', 'F2'])
    expect(first.bulletFacts('b2').map((item) => item.code)).toEqual(['F3', 'F2'])
    expect(first.toEditorPayload()).toEqual({
      b1: [
        { code: 'F1', text: '用 FastAPI 实现问答接口' },
        { code: 'F2', text: '使用 PostgreSQL 和 pgvector' },
      ],
      b2: [
        { code: 'F3', text: '整理 48 条评测集' },
        { code: 'F2', text: '使用 PostgreSQL 和 pgvector' },
      ],
    })
  })
})
