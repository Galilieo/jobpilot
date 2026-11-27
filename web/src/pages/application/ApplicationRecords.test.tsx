import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import type { ApplicationRecord } from '../../api/types'
import { jsonResponse, stubFetch } from '../../test/fetchMock'
import { renderWithProviders } from '../../test/render'
import { ApplicationRecords, ApplicationRow } from './ApplicationRecords'

afterEach(() => vi.unstubAllGlobals())

const jobId = 'job'
const path = '/api/applications/submission'
const listPath = 'GET /api/applications?job_id=job'
const record: ApplicationRecord = {
  application_id: 'submission',
  user_job_id: jobId,
  job_snapshot_json: { title: '示例岗位', company: null, location: null, salary: null },
  resume_version_id: 'v1',
  resume_id: 'resume',
  resume_version: 1,
  fact_set_id: 'facts',
  backfilled: false,
  applied_at: '2026-11-25',
  channel: '官网',
  stage: 'applied',
  revision: 1,
  created_at: '2026-11-25T12:00:00Z',
  updated_at: '2026-11-25T12:00:00Z',
}

it('shows migrated progress without offering a resume snapshot or comparison', async () => {
  stubFetch({
    [listPath]: () => jsonResponse(200, [{ ...record, backfilled: true, resume_version: null }]),
  })
  renderWithProviders(<ApplicationRecords jobId={jobId} />)
  expect(screen.getByRole('link', { name: '在投递页查看' })).toHaveAttribute(
    'href',
    '/applications',
  )
  expect(await screen.findByText('迁移前记录的进度，没有当时的简历')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '查看当时的简历' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '和现在对比' })).not.toBeInTheDocument()
})

it('defaults a new application to today and the latest current version', async () => {
  const user = userEvent.setup()
  const submitted = vi.fn()
  stubFetch({
    [listPath]: () => jsonResponse(200, []),
    'GET /api/jobs/job/tailor-runs': () => jsonResponse(200, [{ run_id: 'run', has_resume: true }]),
    'GET /api/tailor-runs/run/resume': () =>
      jsonResponse(200, {
        current_version_id: 'v2',
        versions: [
          { version: 1, version_id: 'v1', created_at: record.created_at },
          { version: 2, version_id: 'v2', created_at: record.updated_at },
        ],
      }),
    'POST /api/applications': (body) => {
      submitted(body)
      return jsonResponse(201, record)
    },
  })
  renderWithProviders(<ApplicationRecords jobId={jobId} />)
  await user.click(screen.getByRole('button', { name: '记录投递' }))
  const date = await screen.findByLabelText('投递日期')
  const localDate = new Intl.DateTimeFormat('sv-SE').format(new Date())
  expect(date).toHaveValue(localDate)
  await user.type(screen.getByLabelText('投递渠道'), '官网')
  await user.click(screen.getByRole('button', { name: '保存投递' }))
  await waitFor(() =>
    expect(submitted).toHaveBeenCalledWith({
      job_id: jobId,
      resume_version_id: 'v2',
      channel: '官网',
      applied_at: localDate,
    }),
  )
})

it('appends a dated stage change and refreshes an open timeline', async () => {
  const user = userEvent.setup()
  const attempt = vi.fn()
  let latest = record
  stubFetch({
    [listPath]: () => jsonResponse(200, [latest]),
    [`GET ${path}`]: () =>
      jsonResponse(200, {
        ...latest,
        stage_events: [
          {
            event_id: 'initial',
            occurred_at: record.applied_at,
            from_stage: null,
            to_stage: 'applied',
          },
          ...(latest.revision > 1
            ? [
                {
                  event_id: 'test',
                  occurred_at: '2026-11-26',
                  from_stage: 'applied',
                  to_stage: 'written_test',
                },
              ]
            : []),
        ],
      }),
    [`PUT ${path}`]: (body) => {
      attempt(body)
      latest = { ...latest, revision: 2, stage: 'written_test' }
      return jsonResponse(200, latest)
    },
  })
  renderWithProviders(<ApplicationRecords jobId={jobId} />)
  await user.click(await screen.findByText('阶段记录'))
  expect(await screen.findByText('2026-11-25 · 已投递')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '编辑投递' }))
  await user.click(screen.getByRole('combobox', { name: '投递阶段' }))
  await user.click(await screen.findByText('笔试'))
  fireEvent.change(await screen.findByLabelText('阶段发生日期'), {
    target: { value: '2026-11-26' },
  })
  await user.click(screen.getByRole('button', { name: '保存修改' }))
  expect(await screen.findByText('2026-11-26 · 已投递 → 笔试')).toBeInTheDocument()
  expect(attempt).toHaveBeenCalledWith(
    expect.objectContaining({
      expected_revision: 1,
      stage: 'written_test',
      occurred_at: '2026-11-26',
    }),
  )
})

it('reloads on a revision conflict and confirms deletion before sending it', async () => {
  const user = userEvent.setup()
  const remove = vi.fn()
  let latest = [record]
  stubFetch({
    [listPath]: () => jsonResponse(200, latest),
    [`PUT ${path}`]: () => {
      latest = [{ ...record, revision: 2, channel: '内推' }]
      return jsonResponse(409, { detail: 'revision conflict' })
    },
    [`DELETE ${path}`]: (body) => {
      remove(body)
      latest = []
      return new Response(null, { status: 204 })
    },
  })
  renderWithProviders(<ApplicationRecords jobId={jobId} />)
  await user.click(await screen.findByRole('button', { name: '编辑投递' }))
  await user.click(screen.getByRole('button', { name: '保存修改' }))
  expect(await screen.findByText('投递记录刚被别处改过，已重新读取')).toBeInTheDocument()
  expect(await screen.findByText('内推')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '删除投递' }))
  const confirmation = await screen.findByRole('dialog', { name: '删除这条投递记录？' })
  expect(remove).not.toHaveBeenCalled()
  await user.click(within(confirmation).getByRole('button', { name: '删除投递' }))
  expect(await screen.findByText('这个岗位还没有投递记录。')).toBeInTheDocument()
  expect(remove).toHaveBeenCalledWith({ expected_revision: 2 })
})

it('uses the frozen title when the original job is deleted and can hide job info', async () => {
  const deletedRecord = { ...record, user_job_id: null }
  const shown = renderWithProviders(<ApplicationRow record={deletedRecord} refresh={vi.fn()} />)
  expect(await screen.findByText('示例岗位')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '查看当时的简历' })).toBeInTheDocument()
  shown.unmount()

  renderWithProviders(
    <ApplicationRow record={deletedRecord} refresh={vi.fn()} showJobInfo={false} />,
  )
  expect(screen.queryByText('示例岗位')).not.toBeInTheDocument()
})
