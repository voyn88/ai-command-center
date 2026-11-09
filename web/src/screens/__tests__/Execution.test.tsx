import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import Execution from '../Execution'
import { fetchExecution } from '../../lib/api'
import i18n from '../../lib/i18n'

vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return {
    ...actual,
    fetchExecution: vi.fn().mockResolvedValue({
      summary: { visible_runs: 1, active: 1, completed: 0, needs_attention: 0 },
      state_counts: { RUNNING: 1 },
      runs: [{
        id: 'r1', source: 'v2', title: 'implementation', project: 'AICC', project_name: 'AI Command Center',
        task_type: 'implementation', state: 'RUNNING', created_at: '2026-08-03T10:00:00', started_at: '2026-08-03T10:00:00',
        completed_at: null, duration_seconds: null, exit_code: null, failure_reason: null, verdict: null,
      }],
    }),
  }
})

test('renders real execution summary and run rows', async () => {
  render(<Execution onNavigate={vi.fn()} />)

  await waitFor(() => expect(screen.getByText('implementation')).toBeInTheDocument())
  expect(screen.getByText('AI Command Center')).toBeInTheDocument()
  expect(screen.getAllByText('Running now').length).toBeGreaterThan(0)
  expect(screen.queryByText('RUNNING')).not.toBeInTheDocument()
  expect(screen.getByText('Visible runs')).toBeInTheDocument()
})

test('shows plain Russian state labels with the raw id kept in a tooltip', async () => {
  await i18n.changeLanguage('ru')
  render(<Execution onNavigate={vi.fn()} />)

  await waitFor(() => expect(screen.getByText('implementation')).toBeInTheDocument())
  expect(screen.getAllByText('Идёт сейчас').length).toBeGreaterThan(0)
  expect(screen.queryByText('RUNNING')).not.toBeInTheDocument()
  expect(screen.getAllByText('Идёт сейчас')[0]).toHaveAttribute('title', 'RUNNING')
  expect(screen.getByText('Видимые запуски')).toBeInTheDocument()
})

test('error state offers a retry button that reloads', async () => {
  vi.mocked(fetchExecution).mockRejectedValueOnce(new Error('offline'))
  render(<Execution onNavigate={vi.fn()} />)

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Could not load dashboard data.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(screen.getByText('implementation')).toBeInTheDocument())
})

test('empty state explains there are no runs', async () => {
  vi.mocked(fetchExecution).mockResolvedValueOnce({
    summary: { visible_runs: 0, active: 0, completed: 0, needs_attention: 0 },
    state_counts: {},
    runs: [],
  })
  render(<Execution onNavigate={vi.fn()} />)

  expect(await screen.findByText('No visible runs yet.')).toBeInTheDocument()
})

test('failed runs sort first and raise an attention banner', async () => {
  vi.mocked(fetchExecution).mockResolvedValueOnce({
    summary: { visible_runs: 2, active: 1, completed: 0, needs_attention: 1 },
    state_counts: { RUNNING: 1, FAILED: 1 },
    runs: [
      { id: 'a', source: 'v2', title: 'running one', project: 'AICC', project_name: 'AICC', task_type: 'x', state: 'RUNNING', created_at: '2026-08-03T10:00:00', started_at: null, completed_at: null, duration_seconds: 125, exit_code: null, failure_reason: null, verdict: null },
      { id: 'b', source: 'v2', title: 'broken one', project: 'AICC', project_name: 'AICC', task_type: 'x', state: 'FAILED', created_at: '2026-08-03T09:00:00', started_at: null, completed_at: null, duration_seconds: null, exit_code: 1, failure_reason: 'timeout', verdict: null },
    ],
  })
  render(<Execution onNavigate={vi.fn()} />)

  await screen.findByText('broken one')
  expect(screen.getByText('Need attention: 1')).toBeInTheDocument()
  const titles = screen.getAllByRole('article').map((el) => el.querySelector('strong')?.textContent)
  expect(titles).toEqual(['broken one', 'running one'])
  expect(screen.getByText('ran out of time')).toHaveAttribute('title', expect.stringContaining('Exit code: 1'))
  expect(screen.getByText('2 min 5 s')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^All states/ })).toHaveAttribute('aria-pressed', 'true')
})

test('an unknown state shows a neutral phrase, the raw code only in a tooltip', async () => {
  vi.mocked(fetchExecution).mockResolvedValueOnce({
    summary: { visible_runs: 1, active: 0, completed: 0, needs_attention: 0 },
    state_counts: { WAITING_FOR_APPROVAL: 1 },
    runs: [{ id: 'z', source: 'v2', title: 'odd run', project: 'AICC', project_name: 'AICC', task_type: 'x', state: 'WAITING_FOR_APPROVAL', created_at: '2026-08-03T10:00:00', started_at: null, completed_at: null, duration_seconds: null, exit_code: null, failure_reason: 'brand_new_reason', verdict: 'WEIRD_VERDICT' },
    ],
  })
  render(<Execution onNavigate={vi.fn()} />)

  await screen.findByText('odd run')
  expect(screen.queryByText('WAITING_FOR_APPROVAL')).not.toBeInTheDocument()
  expect(screen.queryByText('WEIRD_VERDICT')).not.toBeInTheDocument()
  expect(screen.queryByText(/brand_new_reason/)).not.toBeInTheDocument()
  expect(screen.getAllByText('Status being clarified')[0]).toHaveAttribute('title', 'WAITING_FOR_APPROVAL')
})

test('an empty filtered list offers "Reset filters" that restores the list', async () => {
  vi.mocked(fetchExecution).mockResolvedValueOnce({
    summary: { visible_runs: 2, active: 1, completed: 1, needs_attention: 0 },
    state_counts: { RUNNING: 1, COMPLETED: 1 },
    runs: [
      { id: 'a', source: 'v2', title: 'running one', project: 'AICC', project_name: 'AICC', task_type: 'x', state: 'RUNNING', created_at: '2026-08-03T10:00:00', started_at: null, completed_at: null, duration_seconds: null, exit_code: null, failure_reason: null, verdict: null },
      { id: 'b', source: 'v2', title: 'done one', project: 'AICC', project_name: 'AICC', task_type: 'x', state: 'COMPLETED', created_at: '2026-08-03T09:00:00', started_at: null, completed_at: null, duration_seconds: null, exit_code: 0, failure_reason: null, verdict: 'APPROVED_FOR_COMMIT' },
    ],
  })
  render(<Execution onNavigate={vi.fn()} />)
  await screen.findByText('running one')
  expect(screen.getByText('Approved for commit')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /^Completed/ }))
  expect(screen.queryByText('running one')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /^All states/ }))
  expect(screen.getByText('running one')).toBeInTheDocument()
})
