import { render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import Execution from '../Execution'

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
        provenance: {
          initiated_by: 'kanban_task', prompt: 'Implement feature X', prompt_version: 3, model: 'claude-sonnet-5',
          actions: ['git', 'commit'], reproducibility_hash: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567',
          unknown_fields: [],
        },
      }],
    }),
  }
})

test('renders real execution summary and run rows', async () => {
  render(<Execution onNavigate={vi.fn()} />)

  await waitFor(() => expect(screen.getByText('implementation')).toBeInTheDocument())
  expect(screen.getByText('AI Command Center')).toBeInTheDocument()
  expect(screen.getAllByText('RUNNING').length).toBeGreaterThan(0)
  expect(screen.getByText('Visible runs')).toBeInTheDocument()
})

test('surfaces provenance: initiator, model, and a reproducibility hash', async () => {
  render(<Execution onNavigate={vi.fn()} />)

  await waitFor(() => expect(screen.getByText(/Initiated by: kanban_task/)).toBeInTheDocument())
  expect(screen.getByText(/Model: claude-sonnet-5/)).toBeInTheDocument()
  expect(screen.getByText(/Repro hash: abcdef012345/)).toBeInTheDocument()
})

test('names missing provenance fields as not available, never guessing', async () => {
  const { fetchExecution } = await import('../../lib/api')
  vi.mocked(fetchExecution).mockResolvedValueOnce({
    summary: { visible_runs: 1, active: 0, completed: 1, needs_attention: 0 },
    state_counts: { COMPLETED: 1 },
    runs: [{
      id: 'r2', source: 'v2', title: 'review', project: 'AICC', project_name: 'AI Command Center',
      task_type: 'review', state: 'COMPLETED', created_at: '2026-08-03T10:00:00', started_at: null,
      completed_at: '2026-08-03T10:05:00', duration_seconds: 300, exit_code: 0, failure_reason: null, verdict: 'pass',
      provenance: null,
    }],
  })

  render(<Execution onNavigate={vi.fn()} />)

  await waitFor(() => expect(screen.getByText(/Initiated by: Not available/)).toBeInTheDocument())
  expect(screen.getByText(/Model: Not available/)).toBeInTheDocument()
  expect(screen.getByText(/Repro hash: Not available/)).toBeInTheDocument()
})
