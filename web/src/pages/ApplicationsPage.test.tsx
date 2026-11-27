import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ApplicationListItem } from '../api/types'
import { jsonResponse, stubFetch } from '../test/fetchMock'
import { renderWithProviders } from '../test/render'
import { ApplicationsPage } from './ApplicationsPage'
import { todayLocalDate } from './application/applicationStatus'

function daysAgo(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return todayLocalDate(date)
}

function makeApp(overrides: Partial<ApplicationListItem>): ApplicationListItem {
  const day = daysAgo(2)
  return {
    application_id: 'app-default',
    user_job_id: 'job-default',
    job_snapshot_json: {
      title: '默认岗位',
      company: '默认公司',
      location: '北京',
      salary: null,
    },
    resume_version_id: 'rv-1',
    resume_id: 'r-1',
    resume_version: 1,
    fact_set_id: 'fs-1',
    backfilled: false,
    applied_at: day,
    stage_changed_at: day,
    channel: '官网',
    stage: 'applied',
    revision: 1,
    created_at: '2026-11-20T10:00:00Z',
    updated_at: '2026-11-20T10:00:00Z',
    ...overrides,
  }
}

function renderPage(initialEntry = '/applications') {
  return renderWithProviders(
    <Routes>
      <Route path="/applications" element={<ApplicationsPage />} />
      <Route path="/jobs" element={<div>岗位库页面</div>} />
      <Route path="/jobs/:jobId" element={<div>岗位详情页面</div>} />
    </Routes>,
    { initialEntries: [initialEntry] },
  )
}

describe('ApplicationsPage 投递页', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('看板默认隐藏未通过并显示计数与展开，列内按 stage_changed_at 倒序并展示标记', async () => {
    const user = userEvent.setup()
    const items: ApplicationListItem[] = [
      makeApp({
        application_id: 'app-stale',
        user_job_id: 'job-1',
        job_snapshot_json: {
          title: '大模型实习',
          company: '远帆科技',
          location: '上海',
          salary: null,
        },
        applied_at: daysAgo(20),
        stage_changed_at: daysAgo(20),
        stage: 'applied',
      }),
      makeApp({
        application_id: 'app-fresh',
        user_job_id: 'job-1',
        job_snapshot_json: {
          title: '大模型实习',
          company: '远帆科技',
          location: '上海',
          salary: null,
        },
        applied_at: daysAgo(25),
        stage_changed_at: daysAgo(1),
        stage: 'applied',
      }),
      makeApp({
        application_id: 'app-deleted',
        user_job_id: null,
        job_snapshot_json: {
          title: 'RAG 算法实习',
          company: '岚山研究院',
          location: '深圳',
          salary: null,
        },
        stage: 'interviewing',
      }),
      makeApp({
        application_id: 'app-backfilled',
        user_job_id: 'job-3',
        job_snapshot_json: { title: '平台实习', company: '澄明云', location: '远程', salary: null },
        backfilled: true,
        resume_version: null,
        stage: 'offer',
      }),
      makeApp({
        application_id: 'app-rejected',
        user_job_id: 'job-4',
        job_snapshot_json: {
          title: '已拒岗位',
          company: '星舟软件',
          location: '北京',
          salary: null,
        },
        stage: 'rejected',
      }),
    ]

    stubFetch({
      'GET /api/applications': () => jsonResponse(200, items),
    })

    renderPage()

    expect(await screen.findAllByText('大模型实习')).toHaveLength(2)
    expect(screen.queryByRole('region', { name: '未通过列' })).not.toBeInTheDocument()
    expect(screen.getByText(/另有 1 条未通过/)).toBeInTheDocument()

    // 列内按 stage_changed_at 倒序，app-fresh（1天前改阶段，25天前投递）排在 app-stale（20天前）前面
    const appliedCol = screen.getByRole('region', { name: '已投递列' })
    const cards = within(appliedCol).getAllByRole('button', { name: /大模型实习/ })
    expect(cards[0]).toHaveTextContent('第 1 次投递')
    expect(cards[1]).toHaveTextContent('20 天没有进展')
    expect(cards[1]).toHaveTextContent('第 2 次投递')

    expect(screen.getByText('岗位已删除')).toBeInTheDocument()
    expect(screen.getByText('迁移前记录')).toBeInTheDocument()

    // 点「显示」展开未通过列
    await user.click(screen.getByRole('button', { name: '显示' }))
    expect(await screen.findByRole('region', { name: '未通过列' })).toBeInTheDocument()
    expect(screen.getByText('已拒岗位')).toBeInTheDocument()
  })

  it('列表视图默认按最近变动倒序，可切换到按投递日期排序', async () => {
    const user = userEvent.setup()
    const items = [
      makeApp({
        application_id: 'a-recent-applied',
        job_snapshot_json: {
          title: '最近投递岗位',
          company: '甲公司',
          location: null,
          salary: null,
        },
        applied_at: '2026-11-26',
        stage_changed_at: '2026-11-20',
      }),
      makeApp({
        application_id: 'a-recent-changed',
        job_snapshot_json: {
          title: '最近变动岗位',
          company: '乙公司',
          location: null,
          salary: null,
        },
        applied_at: '2026-11-10',
        stage_changed_at: '2026-11-27',
      }),
    ]
    stubFetch({
      'GET /api/applications': () => jsonResponse(200, items),
    })

    renderPage('/applications?view=list')
    await screen.findByText('最近变动岗位')

    const rowsDefault = screen.getAllByRole('row').slice(1)
    expect(rowsDefault[0]).toHaveTextContent('最近变动岗位')
    expect(rowsDefault[1]).toHaveTextContent('最近投递岗位')

    await user.click(screen.getByRole('button', { name: /投递日期/ }))
    const rowsByApplied = screen.getAllByRole('row').slice(1)
    expect(rowsByApplied[0]).toHaveTextContent('最近投递岗位')
    expect(rowsByApplied[1]).toHaveTextContent('最近变动岗位')
  })

  it('卡片弹层改阶段发送请求并刷新，409 冲突时提示并重新读取', async () => {
    const user = userEvent.setup()
    const putBodies: unknown[] = []
    let latest = makeApp({
      application_id: 'a1',
      job_snapshot_json: { title: 'Agent 实习生', company: '北辰', location: '北京', salary: null },
      stage: 'applied',
      revision: 1,
    })
    let conflictNext = false

    stubFetch({
      'GET /api/applications': () => jsonResponse(200, [latest]),
      'PUT /api/applications/a1': (body) => {
        putBodies.push(body)
        if (conflictNext) {
          latest = { ...latest, revision: 3, stage: 'offer' }
          return jsonResponse(409, { detail: 'conflict' })
        }
        latest = { ...latest, revision: 2, stage: 'interviewing', stage_changed_at: '2026-11-22' }
        return jsonResponse(200, latest)
      },
    })

    renderPage()
    await user.click(await screen.findByRole('button', { name: '改阶段 ▾' }))
    await user.click(screen.getByRole('combobox', { name: '目标阶段' }))
    const option = (await screen.findAllByText('面试中')).find((node) =>
      node.closest('.ant-select-item-option'),
    )
    expect(option).toBeDefined()
    await user.click(option!)
    fireEvent.change(screen.getByLabelText('发生日期'), { target: { value: '2026-11-22' } })
    await user.click(screen.getByRole('button', { name: '改阶段' }))

    await waitFor(() => {
      expect(putBodies[0]).toEqual({
        expected_revision: 1,
        stage: 'interviewing',
        occurred_at: '2026-11-22',
      })
    })
    const interviewingCol = await screen.findByRole('region', { name: '面试中列' })
    expect(within(interviewingCol).getByText('Agent 实习生')).toBeInTheDocument()

    // 409 冲突后重新读取
    conflictNext = true
    await user.click(screen.getByRole('button', { name: '改阶段 ▾' }))
    await user.click(screen.getByRole('button', { name: '改阶段' }))
    expect(await screen.findByText('投递记录刚被别处改过，已重新读取')).toBeInTheDocument()
    const offerCol = await screen.findByRole('region', { name: '已拿 offer列' })
    expect(within(offerCol).getByText('Agent 实习生')).toBeInTheDocument()
  })

  it('列表下拉改阶段取消后恢复原值，确认才发送 PUT', async () => {
    const user = userEvent.setup()
    let latest = makeApp({
      application_id: 'a1',
      job_snapshot_json: { title: '检索实习', company: '青禾', location: null, salary: null },
      stage: 'applied',
      revision: 1,
    })
    const calls = stubFetch({
      'GET /api/applications': () => jsonResponse(200, [latest]),
      'PUT /api/applications/a1': () => {
        latest = { ...latest, revision: 2, stage: 'written_test' }
        return jsonResponse(200, latest)
      },
    })

    renderPage('/applications?view=list')
    const select = await screen.findByRole('combobox', { name: '修改「检索实习」阶段' })
    await user.click(select)
    const testOption1 = (await screen.findAllByText('笔试')).find((node) =>
      node.closest('.ant-select-item-option'),
    )
    await user.click(testOption1!)

    // 弹层出现但不立即发请求，点取消恢复原值
    expect(await screen.findByText('改到「笔试」')).toBeInTheDocument()
    expect(calls.some((call) => call.route === 'PUT /api/applications/a1')).toBe(false)
    await user.click(screen.getByRole('button', { name: /取\s?消/ }))
    await waitFor(() => expect(screen.queryByText('改到「笔试」')).not.toBeInTheDocument(), {
      timeout: 3500,
    })
    expect(within(screen.getAllByRole('row')[1]).getByText('已投递')).toBeInTheDocument()
    expect(calls.some((call) => call.route === 'PUT /api/applications/a1')).toBe(false)

    // 再次选择并确认
    await user.click(select)
    const testOption2 = (await screen.findAllByText('笔试')).find((node) =>
      node.closest('.ant-select-item-option'),
    )
    await user.click(testOption2!)
    await user.click(await screen.findByRole('button', { name: '改阶段' }))
    await waitFor(() =>
      expect(calls.some((call) => call.route === 'PUT /api/applications/a1')).toBe(true),
    )
  })

  it('支持 ?id= 打开抽屉与键盘 Enter 打开，id 不存在时提示并清理参数', async () => {
    const user = userEvent.setup()
    const item = makeApp({
      application_id: 'app-valid',
      user_job_id: 'job-9',
      job_snapshot_json: { title: '可打开投递', company: '星海', location: '杭州', salary: null },
    })
    stubFetch({
      'GET /api/applications': () => jsonResponse(200, [item]),
      'GET /api/applications/app-valid': () =>
        jsonResponse(200, {
          ...item,
          snapshot_json: null,
          stage_events: [
            {
              event_id: 'e1',
              from_stage: null,
              to_stage: 'applied',
              occurred_at: item.applied_at,
              created_at: item.created_at,
            },
          ],
        }),
    })

    const first = renderPage('/applications?id=app-valid')
    expect(await screen.findByRole('link', { name: '打开岗位详情' })).toHaveAttribute(
      'href',
      '/jobs/job-9',
    )
    expect(await screen.findByText(`${item.applied_at} · 已投递`)).toBeInTheDocument()
    first.unmount()

    // 键盘 Enter 打开抽屉
    const second = renderPage('/applications')
    const card = await screen.findByRole('button', { name: /可打开投递/ })
    card.focus()
    await user.keyboard('{Enter}')
    expect(await screen.findByRole('link', { name: '打开岗位详情' })).toBeInTheDocument()
    second.unmount()

    // id 不存在时提示
    renderPage('/applications?id=non-existent')
    expect(await screen.findByText('这条投递不存在或已删除')).toBeInTheDocument()
  })

  it('记录投递两步弹窗：无定制简历时给出说明和链接，有定制简历时选岗位并提交', async () => {
    const user = userEvent.setup()
    let hasResumes = false
    const submitted = vi.fn()

    stubFetch({
      'GET /api/applications': () => jsonResponse(200, []),
      'GET /api/tailored-resumes': () =>
        jsonResponse(
          200,
          hasResumes
            ? [
                {
                  resume_id: 'r1',
                  run_id: 'run-1',
                  job_id: 'job-1',
                  job_title: '后端实习',
                  job_company: '青梧数智',
                  fact_set_version: 1,
                  latest_fact_set_version: 1,
                  current_version: 2,
                  status: 'generated',
                  updated_at: '2026-11-27T10:00:00Z',
                },
              ]
            : [],
        ),
      'GET /api/jobs/job-1/tailor-runs': () =>
        jsonResponse(200, [{ run_id: 'run-1', has_resume: true }]),
      'GET /api/tailor-runs/run-1/resume': () =>
        jsonResponse(200, {
          current_version_id: 'rv-2',
          versions: [{ version: 2, version_id: 'rv-2', created_at: '2026-11-27T10:00:00Z' }],
        }),
      'POST /api/applications': (body) => {
        submitted(body)
        return jsonResponse(201, makeApp({ application_id: 'created-1' }))
      },
    })

    const first = renderPage()
    await user.click(await screen.findByRole('button', { name: '记录投递' }))
    expect(
      await screen.findByText('先在对话里为一个岗位生成定制简历，才能记录投递'),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '去岗位库' })).toHaveAttribute('href', '/jobs')
    first.unmount()

    hasResumes = true
    renderPage()
    await user.click(await screen.findByRole('button', { name: '记录投递' }))
    await user.click(await screen.findByRole('button', { name: /后端实习/ }))
    await user.click(await screen.findByRole('button', { name: '保存投递' }))

    await waitFor(() =>
      expect(submitted).toHaveBeenCalledWith(
        expect.objectContaining({ job_id: 'job-1', resume_version_id: 'rv-2' }),
      ),
    )
  })

  it('无投递或搜索无匹配时显示对应空状态', async () => {
    const user = userEvent.setup()
    let data: ApplicationListItem[] = []
    stubFetch({
      'GET /api/applications': () => jsonResponse(200, data),
    })

    const emptyView = renderPage()
    expect(
      await screen.findByText('还没有投递记录。在岗位详情点「记录投递」，或在对话里说「我投了」。'),
    ).toBeInTheDocument()
    emptyView.unmount()

    data = [
      makeApp({
        application_id: 'a-search',
        job_snapshot_json: { title: '检索实习', company: '北辰智能', location: null, salary: null },
      }),
    ]
    renderPage()
    await screen.findByText('检索实习')
    await user.type(screen.getByPlaceholderText('搜岗位名或公司'), '不存在的公司')
    expect(screen.getByText('没有匹配的投递。')).toBeInTheDocument()
  })
})
