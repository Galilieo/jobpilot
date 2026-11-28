// 岗位库筛选、搜索参数序列化、各维度选项计数与排序逻辑（纯函数）。
import type { JobStage, JobSummary } from '../../api/types'
import { JOB_STAGE } from '../../labels'

export type JobSort = 'updated' | 'created'

export interface JobFilterState {
  q: string
  directions: string[]
  cities: string[]
  techs: string[]
  stages: JobStage[]
  sort: JobSort
}

export interface FacetOption {
  value: string
  count: number
  label: string
}

export interface FacetOptionGroups {
  directions: FacetOption[]
  cities: FacetOption[]
  techs: FacetOption[]
  stages: FacetOption[]
}

const VALID_STAGES = new Set<string>(Object.keys(JOB_STAGE))
const TECH_OPTION_LIMIT = 15

function uniqueValues(values: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const raw of values) {
    const trimmed = raw.trim()
    if (!trimmed || seen.has(trimmed)) continue
    seen.add(trimmed)
    result.push(trimmed)
  }
  return result
}

export function parseJobFilters(params: URLSearchParams): JobFilterState {
  const q = (params.get('q') ?? '').trim()
  const directions = uniqueValues(params.getAll('direction'))
  const cities = uniqueValues(params.getAll('city'))
  const techs = uniqueValues(params.getAll('tech'))
  const stages = uniqueValues(params.getAll('stage')).filter((s): s is JobStage =>
    VALID_STAGES.has(s),
  )
  const sort: JobSort = params.get('sort') === 'created' ? 'created' : 'updated'
  return { q, directions, cities, techs, stages, sort }
}

export function buildJobSearchParams(state: JobFilterState): URLSearchParams {
  const params = new URLSearchParams()
  const q = state.q.trim()
  if (q) params.set('q', q)
  for (const value of uniqueValues(state.directions)) params.append('direction', value)
  for (const value of uniqueValues(state.cities)) params.append('city', value)
  for (const value of uniqueValues(state.techs)) params.append('tech', value)
  for (const value of uniqueValues(state.stages)) params.append('stage', value)
  if (state.sort === 'created') params.set('sort', 'created')
  return params
}

export function hasActiveJobFilters(state: JobFilterState): boolean {
  return (
    Boolean(state.q) ||
    state.directions.length > 0 ||
    state.cities.length > 0 ||
    state.techs.length > 0 ||
    state.stages.length > 0
  )
}

function countByValues(
  jobs: JobSummary[],
  pick: (job: JobSummary) => string[],
): Map<string, number> {
  const counts = new Map<string, number>()
  for (const job of jobs) {
    for (const value of new Set(pick(job))) {
      counts.set(value, (counts.get(value) ?? 0) + 1)
    }
  }
  return counts
}

function toSortedOptions(
  counts: Map<string, number>,
  selected: string[],
  tailOrder: readonly string[],
): FacetOption[] {
  const merged = new Map(counts)
  for (const item of selected) {
    if (!merged.has(item)) merged.set(item, 0)
  }
  const tailRank = (value: string): number => {
    const idx = tailOrder.indexOf(value)
    return idx === -1 ? -1 : idx
  }
  return Array.from(merged.entries())
    .sort(([a, countA], [b, countB]) => {
      const rankA = tailRank(a)
      const rankB = tailRank(b)
      if (rankA !== rankB) return rankA - rankB
      if (countB !== countA) return countB - countA
      return a.localeCompare(b, 'zh-CN')
    })
    .map(([value, count]) => ({ value, count, label: `${value}（${count}）` }))
}

export function buildFacetOptions(
  jobs: JobSummary[],
  selected: Pick<JobFilterState, 'directions' | 'cities' | 'techs' | 'stages'>,
): FacetOptionGroups {
  const directionCounts = countByValues(jobs, (job) => job.directions ?? [])
  const cityCounts = countByValues(jobs, (job) => job.cities ?? [])
  const techCounts = countByValues(jobs, (job) => job.keywords ?? [])

  const topTechs = Array.from(techCounts.entries())
    .sort(([a, countA], [b, countB]) =>
      countB !== countA ? countB - countA : a.localeCompare(b, 'zh-CN'),
    )
    .slice(0, TECH_OPTION_LIMIT)
  const techValues = new Set(topTechs.map(([name]) => name))
  const techEntries = [...topTechs]
  for (const chosen of selected.techs) {
    if (!techValues.has(chosen)) techEntries.push([chosen, techCounts.get(chosen) ?? 0])
  }

  const stageCounts = countByValues(jobs, (job) => [job.stage])
  const stages = (Object.keys(JOB_STAGE) as JobStage[]).map((value) => {
    const count = stageCounts.get(value) ?? 0
    return { value, count, label: `${JOB_STAGE[value]}（${count}）` }
  })

  return {
    directions: toSortedOptions(directionCounts, selected.directions, ['其他']),
    cities: toSortedOptions(cityCounts, selected.cities, ['多地', '其他']),
    techs: techEntries.map(([value, count]) => ({
      value,
      count,
      label: `${value}（${count}）`,
    })),
    stages,
  }
}

function matchesAny(selected: string[], values: string[]): boolean {
  return selected.length === 0 || selected.some((item) => values.includes(item))
}

function timestampOf(iso: string | undefined): number {
  if (!iso) return 0
  const parsed = Date.parse(iso)
  return Number.isNaN(parsed) ? 0 : parsed
}

export function filterAndSortJobs(jobs: JobSummary[], state: JobFilterState): JobSummary[] {
  const filtered = jobs.filter(
    (job) =>
      matchesAny(state.directions, job.directions ?? []) &&
      matchesAny(state.cities, job.cities ?? []) &&
      matchesAny(state.techs, job.keywords ?? []) &&
      (state.stages.length === 0 || state.stages.includes(job.stage)),
  )
  return [...filtered].sort((a, b) => {
    const fieldA = state.sort === 'created' ? a.created_at || a.updated_at : a.updated_at
    const fieldB = state.sort === 'created' ? b.created_at || b.updated_at : b.updated_at
    return timestampOf(fieldB) - timestampOf(fieldA)
  })
}
