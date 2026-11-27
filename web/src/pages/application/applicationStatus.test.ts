import { describe, expect, it } from 'vitest'
import type { ApplicationListItem } from '../../api/types'
import {
  STALE_AFTER_DAYS,
  applicationAttemptNumbers,
  daysBetweenDates,
  isStaleApplication,
  sortApplicationsForList,
  sortByStageChangedDesc,
  summarizeApplications,
} from './applicationStatus'

function makeItem(overrides: Partial<ApplicationListItem>): ApplicationListItem {
  return {
    application_id: 'app-1',
    user_job_id: 'job-1',
    job_snapshot_json: {
      title: '后端开发实习生',
      company: '示例公司',
      location: '上海',
      salary: null,
    },
    resume_version_id: 'rv-1',
    resume_id: 'r-1',
    resume_version: 1,
    fact_set_id: 'fs-1',
    backfilled: false,
    applied_at: '2026-11-10',
    stage_changed_at: '2026-11-10',
    channel: '官网',
    stage: 'applied',
    revision: 1,
    created_at: '2026-11-10T10:00:00Z',
    updated_at: '2026-11-10T10:00:00Z',
    ...overrides,
  }
}

describe('applicationStatus 纯函数规则', () => {
  it('按本地日期字符串计算天数差，跨月跨年不受时区影响', () => {
    expect(daysBetweenDates('2026-11-27', '2026-11-27')).toBe(0)
    expect(daysBetweenDates('2026-01-31', '2026-02-02')).toBe(2)
    expect(daysBetweenDates('2024-02-28', '2024-03-01')).toBe(2)
    expect(daysBetweenDates('2025-12-25', '2026-01-10')).toBe(16)
  })

  it('恰好 14 天不标没进展，超过 14 天标出；Offer 与未通过不标', () => {
    expect(STALE_AFTER_DAYS).toBe(14)
    const today = '2026-11-27'

    expect(
      isStaleApplication(makeItem({ stage: 'applied', stage_changed_at: '2026-11-13' }), today),
    ).toBe(false)
    expect(
      isStaleApplication(makeItem({ stage: 'applied', stage_changed_at: '2026-11-12' }), today),
    ).toBe(true)
    expect(
      isStaleApplication(
        makeItem({ stage: 'written_test', stage_changed_at: '2026-11-12' }),
        today,
      ),
    ).toBe(true)
    expect(
      isStaleApplication(
        makeItem({ stage: 'interviewing', stage_changed_at: '2026-11-12' }),
        today,
      ),
    ).toBe(true)

    expect(
      isStaleApplication(makeItem({ stage: 'offer', stage_changed_at: '2026-10-01' }), today),
    ).toBe(false)
    expect(
      isStaleApplication(makeItem({ stage: 'rejected', stage_changed_at: '2026-10-01' }), today),
    ).toBe(false)
  })

  it('同一岗位多次投递按投递与创建时间正序编为第 N 次，单次与已删除岗位不编号', () => {
    const a1 = makeItem({
      application_id: 'a1',
      user_job_id: 'job-a',
      applied_at: '2026-11-01',
      created_at: '2026-11-01T10:00:00Z',
    })
    const a2 = makeItem({
      application_id: 'a2',
      user_job_id: 'job-a',
      applied_at: '2026-11-20',
      created_at: '2026-11-20T09:00:00Z',
    })
    const a3 = makeItem({
      application_id: 'a3',
      user_job_id: 'job-a',
      applied_at: '2026-11-20',
      created_at: '2026-11-20T12:00:00Z',
    })
    const single = makeItem({ application_id: 's1', user_job_id: 'job-b' })
    const deleted1 = makeItem({ application_id: 'd1', user_job_id: null })
    const deleted2 = makeItem({ application_id: 'd2', user_job_id: null })

    // 传入乱序数组，仍按正序编号
    const numbers = applicationAttemptNumbers([a3, deleted1, a1, single, a2, deleted2])
    expect(numbers.get('a1')).toBe(1)
    expect(numbers.get('a2')).toBe(2)
    expect(numbers.get('a3')).toBe(3)
    expect(numbers.has('s1')).toBe(false)
    expect(numbers.has('d1')).toBe(false)
    expect(numbers.has('d2')).toBe(false)
  })

  it('看板列内按 stage_changed_at 倒序，列表支持按最近变动与投递日期切换排序', () => {
    const olderChangedNewerApplied = makeItem({
      application_id: 'x',
      applied_at: '2026-11-25',
      stage_changed_at: '2026-11-20',
    })
    const newerChangedOlderApplied = makeItem({
      application_id: 'y',
      applied_at: '2026-11-10',
      stage_changed_at: '2026-11-26',
    })

    expect(
      sortByStageChangedDesc([olderChangedNewerApplied, newerChangedOlderApplied]).map(
        (item) => item.application_id,
      ),
    ).toEqual(['y', 'x'])

    expect(
      sortApplicationsForList(
        [olderChangedNewerApplied, newerChangedOlderApplied],
        'stage_changed_at',
      ).map((item) => item.application_id),
    ).toEqual(['y', 'x'])

    expect(
      sortApplicationsForList(
        [olderChangedNewerApplied, newerChangedOlderApplied],
        'applied_at',
      ).map((item) => item.application_id),
    ).toEqual(['x', 'y'])
  })

  it('汇总条统计总投递、等结果、超 14 天未进展与 Offer 数', () => {
    const today = '2026-11-27'
    const items = [
      makeItem({ application_id: '1', stage: 'applied', stage_changed_at: '2026-11-10' }),
      makeItem({ application_id: '2', stage: 'written_test', stage_changed_at: '2026-11-25' }),
      makeItem({ application_id: '3', stage: 'interviewing', stage_changed_at: '2026-11-01' }),
      makeItem({ application_id: '4', stage: 'offer', stage_changed_at: '2026-10-01' }),
      makeItem({ application_id: '5', stage: 'rejected', stage_changed_at: '2026-10-01' }),
    ]
    expect(summarizeApplications(items, today)).toEqual({
      total: 5,
      waiting: 3,
      stale: 2,
      offer: 1,
    })
  })
})
