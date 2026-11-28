// 岗位库顶部搜索框、多维下拉筛选、排序与已选条件标签栏。
import { SearchOutlined } from '@ant-design/icons'
import { Button, Input, Select, Tag } from 'antd'
import { useEffect, useRef, useState, type ReactElement } from 'react'
import type { JobStage } from '../../api/types'
import { JOB_STAGE } from '../../labels'
import {
  hasActiveJobFilters,
  type FacetOptionGroups,
  type JobFilterState,
  type JobSort,
} from './jobFilters'
import './jobsFilter.css'

const SEARCH_DEBOUNCE_MS = 300

interface JobsFilterBarProps {
  filters: JobFilterState
  options: FacetOptionGroups
  onChange: (next: JobFilterState, replace?: boolean) => void
  onClearAll: () => void
}

function removeItem<T>(list: T[], target: T): T[] {
  return list.filter((item) => item !== target)
}

export function JobsFilterBar({
  filters,
  options,
  onChange,
  onClearAll,
}: JobsFilterBarProps): ReactElement {
  const [searchText, setSearchText] = useState(filters.q)
  const [syncedQ, setSyncedQ] = useState(filters.q)
  const timerRef = useRef<number | undefined>(undefined)
  const latestFiltersRef = useRef(filters)

  if (syncedQ !== filters.q) {
    setSyncedQ(filters.q)
    setSearchText(filters.q)
  }

  useEffect(() => {
    latestFiltersRef.current = filters
  }, [filters])

  useEffect(() => () => window.clearTimeout(timerRef.current), [filters.q])

  function commitSearch(raw: string, replace = true): void {
    window.clearTimeout(timerRef.current)
    const nextQ = raw.trim()
    const current = latestFiltersRef.current
    if (nextQ !== current.q) onChange({ ...current, q: nextQ }, replace)
  }

  function handleSearchChange(value: string): void {
    setSearchText(value)
    window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => commitSearch(value, true), SEARCH_DEBOUNCE_MS)
  }

  const active = hasActiveJobFilters(filters)

  return (
    <>
      <div className="job-toolbar">
        <Input
          allowClear
          maxLength={60}
          className="job-search-input"
          prefix={<SearchOutlined />}
          placeholder="搜岗位名、公司、技术或 JD 原文"
          aria-label="搜岗位名、公司、技术或 JD 原文"
          value={searchText}
          onChange={(event) => handleSearchChange(event.target.value)}
          onPressEnter={() => commitSearch(searchText, true)}
        />
        <Select<string[]>
          mode="multiple"
          allowClear
          maxTagCount={1}
          optionFilterProp="label"
          className="job-facet-select"
          placeholder="方向"
          aria-label="按方向筛选"
          value={filters.directions}
          options={options.directions}
          onChange={(directions) => onChange({ ...filters, directions })}
        />
        <Select<string[]>
          mode="multiple"
          allowClear
          maxTagCount={1}
          optionFilterProp="label"
          className="job-facet-select"
          placeholder="城市"
          aria-label="按城市筛选"
          value={filters.cities}
          options={options.cities}
          onChange={(cities) => onChange({ ...filters, cities })}
        />
        <Select<string[]>
          mode="multiple"
          allowClear
          maxTagCount={1}
          optionFilterProp="label"
          className="job-facet-select"
          placeholder="技术"
          aria-label="按技术筛选"
          value={filters.techs}
          options={options.techs}
          onChange={(techs) => onChange({ ...filters, techs })}
        />
        <Select<JobStage[]>
          mode="multiple"
          allowClear
          maxTagCount={1}
          optionFilterProp="label"
          className="job-facet-select"
          placeholder="进度"
          aria-label="按岗位进度筛选"
          value={filters.stages}
          options={options.stages}
          onChange={(stages) => onChange({ ...filters, stages })}
        />
        <Select<JobSort>
          className="job-sort-select"
          aria-label="排序方式"
          value={filters.sort}
          options={[
            { value: 'updated', label: '最近更新' },
            { value: 'created', label: '最近添加' },
          ]}
          onChange={(sort) => onChange({ ...filters, sort })}
        />
      </div>

      {active && (
        <div className="job-active-filters" aria-label="已选筛选条件">
          {filters.q && (
            <Tag
              closable
              onClose={(event) => {
                event.preventDefault()
                setSearchText('')
                onChange({ ...filters, q: '' })
              }}
            >
              搜索：{filters.q}
            </Tag>
          )}
          {filters.directions.map((value) => (
            <Tag
              key={`direction:${value}`}
              closable
              onClose={(event) => {
                event.preventDefault()
                onChange({ ...filters, directions: removeItem(filters.directions, value) })
              }}
            >
              方向：{value}
            </Tag>
          ))}
          {filters.cities.map((value) => (
            <Tag
              key={`city:${value}`}
              closable
              onClose={(event) => {
                event.preventDefault()
                onChange({ ...filters, cities: removeItem(filters.cities, value) })
              }}
            >
              城市：{value}
            </Tag>
          ))}
          {filters.techs.map((value) => (
            <Tag
              key={`tech:${value}`}
              closable
              onClose={(event) => {
                event.preventDefault()
                onChange({ ...filters, techs: removeItem(filters.techs, value) })
              }}
            >
              技术：{value}
            </Tag>
          ))}
          {filters.stages.map((value) => (
            <Tag
              key={`stage:${value}`}
              closable
              onClose={(event) => {
                event.preventDefault()
                onChange({ ...filters, stages: removeItem(filters.stages, value) })
              }}
            >
              进度：{JOB_STAGE[value]}
            </Tag>
          ))}
          <Button type="link" size="small" onClick={onClearAll}>
            清除全部
          </Button>
        </div>
      )}
    </>
  )
}
