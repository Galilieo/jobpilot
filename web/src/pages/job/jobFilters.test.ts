import { describe, expect, it } from 'vitest'
import type { JobSummary } from '../../api/types'
import {
  buildFacetOptions,
  buildJobSearchParams,
  filterAndSortJobs,
  hasActiveJobFilters,
  parseJobFilters,
  type JobFilterState,
} from './jobFilters'

function makeJob(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    job_id: 'job-1',
    title: '后端实习生',
    company: '星桥数据',
    salary: '250 元/天',
    location: '上海',
    onsite_days_per_week: '4 天/周',
    education: '本科',
    stage: 'saved',
    keywords: ['Go', 'Redis'],
    directions: ['后端'],
    cities: ['上海'],
    injection_count: 0,
    match_summary: null,
    created_at: '2026-11-20T10:00:00Z',
    updated_at: '2026-11-25T10:00:00Z',
    ...overrides,
  }
}

describe('jobFilters 纯函数', () => {
  it('解析并序列化 URL 参数，忽略非法阶段且省略默认排序', () => {
    const parsed = parseJobFilters(
      new URLSearchParams(
        'q=+FastAPI+&direction=后端&direction=AI 应用&direction=后端&city=上海&tech=Python&stage=saved&stage=invalid&sort=created',
      ),
    )
    expect(parsed).toEqual({
      q: 'FastAPI',
      directions: ['后端', 'AI 应用'],
      cities: ['上海'],
      techs: ['Python'],
      stages: ['saved'],
      sort: 'created',
    })
    expect(hasActiveJobFilters(parsed)).toBe(true)

    const serialized = buildJobSearchParams(parsed)
    expect(parseJobFilters(serialized)).toEqual(parsed)

    const defaultSortParams = buildJobSearchParams({ ...parsed, sort: 'updated' })
    expect(defaultSortParams.has('sort')).toBe(false)

    const empty = parseJobFilters(new URLSearchParams())
    expect(hasActiveJobFilters(empty)).toBe(false)
    expect(buildJobSearchParams(empty).toString()).toBe('')
  })

  it('支持同维度 OR、跨维度 AND 筛选以及最近更新/最近添加排序', () => {
    const jobs = [
      makeJob({
        job_id: 'j1',
        directions: ['AI 应用'],
        cities: ['上海'],
        keywords: ['RAG', 'Python'],
        stage: 'saved',
        created_at: '2026-11-24T10:00:00Z',
        updated_at: '2026-11-25T10:00:00Z',
      }),
      makeJob({
        job_id: 'j2',
        directions: ['后端'],
        cities: ['北京', '上海'],
        keywords: ['Go', 'Redis'],
        stage: 'applied',
        created_at: '2026-11-26T10:00:00Z',
        updated_at: '2026-11-26T08:00:00Z',
      }),
      makeJob({
        job_id: 'j3',
        directions: ['前端'],
        cities: ['杭州'],
        keywords: ['React'],
        stage: 'saved',
        created_at: '2026-11-27T10:00:00Z',
        updated_at: '2026-11-26T09:00:00Z',
      }),
    ]

    const base: JobFilterState = {
      q: '',
      directions: [],
      cities: [],
      techs: [],
      stages: [],
      sort: 'updated',
    }

    expect(filterAndSortJobs(jobs, base).map((j) => j.job_id)).toEqual(['j3', 'j2', 'j1'])
    expect(filterAndSortJobs(jobs, { ...base, sort: 'created' }).map((j) => j.job_id)).toEqual([
      'j3',
      'j2',
      'j1',
    ])

    // j1 在 j3 之前按 created 算仍是 j3,j2,j1；调整 j1 的 created_at 更晚时应排第一
    const withNewerCreated = jobs.map((j) =>
      j.job_id === 'j1' ? { ...j, created_at: '2026-11-28T10:00:00Z' } : j,
    )
    expect(
      filterAndSortJobs(withNewerCreated, { ...base, sort: 'created' }).map((j) => j.job_id),
    ).toEqual(['j1', 'j3', 'j2'])

    // 同维度 OR（AI 应用 或 后端）+ 城市（上海）+ 阶段（saved 或 applied）
    const combined = filterAndSortJobs(jobs, {
      ...base,
      directions: ['AI 应用', '后端'],
      cities: ['上海'],
      stages: ['saved', 'applied'],
    })
    expect(combined.map((j) => j.job_id)).toEqual(['j2', 'j1'])

    // 再叠加上技术筛选 Go -> 只剩 j2
    const withTech = filterAndSortJobs(jobs, {
      ...base,
      directions: ['AI 应用', '后端'],
      cities: ['上海'],
      techs: ['Go'],
    })
    expect(withTech.map((j) => j.job_id)).toEqual(['j2'])
  })

  it('构建筛选计数：技术最多列前 15 个，已选但数量为 0 的项仍保留', () => {
    const manyTechs = Array.from({ length: 18 }, (_, i) => `Tech${String(i + 1).padStart(2, '0')}`)
    const jobs = [
      makeJob({
        job_id: 'j1',
        directions: ['AI 应用'],
        cities: ['上海'],
        keywords: manyTechs,
        stage: 'saved',
      }),
      makeJob({
        job_id: 'j2',
        directions: ['AI 应用', '后端'],
        cities: ['北京'],
        keywords: manyTechs.slice(0, 5),
        stage: 'interviewing',
      }),
    ]

    const options = buildFacetOptions(jobs, {
      directions: ['前端'],
      cities: ['成都'],
      techs: ['Tech18'],
      stages: ['offer'],
    })

    expect(options.directions).toEqual([
      { value: 'AI 应用', count: 2, label: 'AI 应用（2）' },
      { value: '后端', count: 1, label: '后端（1）' },
      { value: '前端', count: 0, label: '前端（0）' },
    ])
    expect(options.cities).toEqual([
      { value: '北京', count: 1, label: '北京（1）' },
      { value: '上海', count: 1, label: '上海（1）' },
      { value: '成都', count: 0, label: '成都（0）' },
    ])
    // 前 15 个加已选不在前 15 的 Tech18
    expect(options.techs).toHaveLength(16)
    expect(options.techs.slice(0, 5).map((t) => t.value)).toEqual([
      'Tech01',
      'Tech02',
      'Tech03',
      'Tech04',
      'Tech05',
    ])
    expect(options.techs[15]).toEqual({ value: 'Tech18', count: 1, label: 'Tech18（1）' })
    expect(options.stages.find((s) => s.value === 'saved')?.label).toBe('已收藏（1）')
    expect(options.stages.find((s) => s.value === 'interviewing')?.label).toBe('面试中（1）')
  })
})
