import { screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ApplicationDiff } from '../../api/types'
import { jsonResponse, stubFetch } from '../../test/fetchMock'
import { renderWithProviders } from '../../test/render'
import { ApplicationDiffDrawer } from './ApplicationDiffDrawer'

afterEach(() => vi.unstubAllGlobals())

const base: ApplicationDiff = {
  source_deleted: false,
  regenerated: false,
  regenerated_resume_id: null,
  regenerated_content: null,
  resume_changes: [],
  fact_changes: [],
  risks: [],
}

function show(diff: ApplicationDiff): void {
  stubFetch({ 'GET /api/applications/id/diff': () => jsonResponse(200, diff) })
  renderWithProviders(<ApplicationDiffDrawer id="id" close={vi.fn()} />)
}

it('renders both sides of a change in the same resume', async () => {
  show({
    ...base,
    resume_changes: [{ item_id: 'bullet', before: '耗时降低 30%', after: '耗时降低 20%' }],
  })
  expect(await screen.findByText('耗时降低 30%')).toBeInTheDocument()
  expect(screen.getByText('耗时降低 20%')).toBeInTheDocument()
})

it('shows the original snapshot beside a regenerated resume', async () => {
  show({
    ...base,
    regenerated: true,
    regenerated_resume_id: 'new',
    regenerated_content: {
      header: ['新简历页头'],
      sections: [],
      style: { font: 'default', leading: 1.5, size_pt: 11 },
    },
  })
  expect(await screen.findByText('新简历页头')).toBeInTheDocument()
  expect(screen.getByTitle('投递时选定的简历')).toHaveAttribute(
    'src',
    '/api/applications/id/snapshot.html',
  )
})

it('explains a deleted source and still shows the fact changes', async () => {
  show({
    ...base,
    source_deleted: true,
    fact_changes: [{ item_id: 'fact', before: '旧事实', after: null }],
  })
  expect(await screen.findByText('原定制简历已删除，仍可查看当时的快照。')).toBeInTheDocument()
  expect(screen.getByText('旧事实')).toBeInTheDocument()
})
