import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, test, expect, vi, beforeEach } from 'vitest'
import Tasks from '../Tasks'
import { fetchHome } from '../../lib/api'
import type { HomeDTO } from '../../lib/api'
import {
  enqueueAudit,
  fetchQueueItems,
  getOwnerToken,
  QueueAuthError,
} from '../../lib/queueApi'
import type { QueueItem } from '../../lib/queueApi'

vi.mock('../../lib/api', () => ({ fetchHome: vi.fn() }))

vi.mock('../../lib/queueApi', async () => {
  const actual = await vi.importActual<typeof import('../../lib/queueApi')>('../../lib/queueApi')
  return {
    ...actual,
    fetchQueueItems: vi.fn(),
    fetchQueueItem: vi.fn(),
    enqueueAudit: vi.fn(),
    getOwnerToken: vi.fn(),
  }
})

const home: HomeDTO = {
  projects: [
    { id: 'AICC', name: 'AI Command Center', healthy: true },
    { id: 'AIOS', name: 'AIOS', healthy: true },
  ],
  kpis: {
    projects: { value: 2, meta_key: 'all_healthy', meta_n: 2 },
    agents: { value: 0, meta_key: 'running', meta_n: 0 },
    tasks: { value: 0, meta_key: 'in_progress', meta_n: 0 },
    reviews: { value: 0, meta_key: 'pending', meta_n: 0 },
  },
  queue: [],
  health: { projects_healthy: 2, projects_total: 2 },
  activity: [],
  overview: { reports_count: 0, artifacts_count: 0, recent_activity_count: 0 },
  status: [],
}

const oneItem: QueueItem[] = [
  {
    work_item_id: 'wki_1',
    queue: 'execution',
    idempotency_key: 'audit-abc',
    task_id: null,
    repository_id: 'AICC',
    priority: 0,
    available_at: null,
    state: 'ready',
    attempt_count: 0,
    max_attempts: 3,
    current_attempt_id: null,
    result_id: null,
    dead_reason: null,
    dead_at: null,
    created_at: '2026-08-26T10:00:00Z',
    updated_at: '2026-08-26T10:00:00Z',
  },
]

beforeEach(() => {
  vi.mocked(fetchHome).mockReset().mockResolvedValue(home)
  vi.mocked(fetchQueueItems).mockReset().mockResolvedValue({ items: oneItem })
  vi.mocked(enqueueAudit).mockReset()
  vi.mocked(getOwnerToken).mockReset().mockReturnValue('secret-token')
})

describe('Tasks — one-button audit', () => {
  test('renders a project picker and enqueues an audit on submit', async () => {
    vi.mocked(enqueueAudit).mockResolvedValue({ work_item_id: 'wki_new', idempotency_key: 'audit-abc' })
    render(<Tasks onNavigate={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('Run audit')).toBeInTheDocument())
    expect(screen.getByLabelText('Project')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Run audit'))

    await waitFor(() => expect(enqueueAudit).toHaveBeenCalledTimes(1))
    const call = vi.mocked(enqueueAudit).mock.calls[0][0]
    expect(call.project_id).toBe('AICC')
    expect(call.prompt.length).toBeGreaterThan(0)
    expect(call.repository_path).toBeUndefined()

    // The list reloads after a successful enqueue.
    await waitFor(() => expect(fetchQueueItems).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Audit queued — it will appear in the list below.')).toBeInTheDocument()
  })

  test('shows the locked message when the owner token is rejected mid-submit', async () => {
    vi.mocked(enqueueAudit).mockRejectedValue(new QueueAuthError())
    render(<Tasks onNavigate={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('Run audit')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Run audit'))

    expect(await screen.findByText(/Access key missing or rejected/)).toBeInTheDocument()
  })

  test('does not render the audit launcher when there are no projects', async () => {
    vi.mocked(fetchHome).mockResolvedValue({ ...home, projects: [] })
    render(<Tasks onNavigate={vi.fn()} />)

    // The task list still renders (queue reads are independent of the
    // project picker), proving the screen settled before asserting absence.
    await waitFor(() => expect(screen.getByText('Task: AICC')).toBeInTheDocument())
    expect(screen.queryByText('Run audit')).not.toBeInTheDocument()
  })
})
