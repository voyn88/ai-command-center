import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, test, expect, vi, beforeEach } from 'vitest'
import Tasks from '../Tasks'
import { fetchHome } from '../../lib/api'
import type { HomeDTO } from '../../lib/api'
import {
  confirmBacklogTask,
  draftBacklogTask,
  fetchBacklogStatus,
  fetchBacklogTaskDetail,
  fetchBacklogTasks,
  reassignBacklogTask,
} from '../../lib/backlogApi'
import type { BacklogTask } from '../../lib/backlogApi'
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

vi.mock('../../lib/backlogApi', async () => {
  const actual = await vi.importActual<typeof import('../../lib/backlogApi')>('../../lib/backlogApi')
  return {
    ...actual,
    fetchBacklogStatus: vi.fn(),
    fetchBacklogTasks: vi.fn(),
    fetchBacklogTaskDetail: vi.fn(),
    draftBacklogTask: vi.fn(),
    confirmBacklogTask: vi.fn(),
    reassignBacklogTask: vi.fn(),
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

const backlogTask: BacklogTask = {
  task_id: 'VOYN-W0-EXAMPLE',
  wave: '0',
  priority: 'P0',
  status: 'OPEN',
  kind: 'feature',
  title: 'Ship the example',
  repo: 'ai-command-center',
  revision: 1,
}

beforeEach(() => {
  vi.mocked(fetchHome).mockReset().mockResolvedValue(home)
  vi.mocked(fetchQueueItems).mockReset().mockResolvedValue({ items: oneItem })
  vi.mocked(enqueueAudit).mockReset()
  vi.mocked(getOwnerToken).mockReset().mockReturnValue('secret-token')
  vi.mocked(fetchBacklogStatus).mockReset().mockResolvedValue({ counts: { OPEN: 1 }, total: 1 })
  vi.mocked(fetchBacklogTasks).mockReset().mockResolvedValue({ tasks: [backlogTask], total: 1 })
  vi.mocked(fetchBacklogTaskDetail).mockReset().mockResolvedValue({ task: backlogTask, events: [], evidence: [] })
  vi.mocked(draftBacklogTask).mockReset()
  vi.mocked(confirmBacklogTask).mockReset()
  vi.mocked(reassignBacklogTask).mockReset()
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

describe('Tasks — backlog decomposition and progress (S6c)', () => {
  test('renders the done/total progress bar and the task list', async () => {
    render(<Tasks onNavigate={vi.fn()} />)

    expect(await screen.findByText('Ship the example')).toBeInTheDocument()
    // The fixture task is OPEN, not DONE, so the bar reads 0 of 1 done.
    expect(
      screen.getByText((_, element) => element?.tagName === 'SPAN' && element.textContent === '0% Done (0/1)'),
    ).toBeInTheDocument()
  })

  test('shows a retry control when the backlog fails to load', async () => {
    vi.mocked(fetchBacklogStatus).mockRejectedValue(new Error('boom'))
    render(<Tasks onNavigate={vi.fn()} />)

    expect(await screen.findByText('Could not load the backlog.')).toBeInTheDocument()
  })
})

describe('Tasks — chat-text backlog intake (S6a)', () => {
  test('drafts a line, then confirms it and reloads the backlog', async () => {
    vi.mocked(draftBacklogTask).mockResolvedValue({
      ok: true,
      line: '- **VOYN-NEW** | Wave 0 | OPEN | P1 | | `slug` | a new task',
      task: {
        task_id: 'VOYN-NEW',
        wave: '0',
        priority: 'P1',
        status: 'OPEN',
        kind: 'feature',
        title: 'a new task',
        body: 'a new task',
        repo: null,
      },
    })
    vi.mocked(confirmBacklogTask).mockResolvedValue({ task_id: 'VOYN-NEW', reason: 'inserted', changed: true })
    render(<Tasks onNavigate={vi.fn()} />)

    const textarea = await screen.findByLabelText('Add or reprioritize by chat')
    fireEvent.change(textarea, { target: { value: 'add a new task, wave 0, P1' } })
    fireEvent.click(screen.getByText('Propose'))

    await waitFor(() => expect(draftBacklogTask).toHaveBeenCalledWith('add a new task, wave 0, P1'))
    const preview = await screen.findByLabelText('Proposed line — edit if needed, then confirm:')
    expect(preview).toHaveValue('- **VOYN-NEW** | Wave 0 | OPEN | P1 | | `slug` | a new task')

    fireEvent.click(screen.getByText('Confirm'))

    await waitFor(() => expect(confirmBacklogTask).toHaveBeenCalledWith(
      '- **VOYN-NEW** | Wave 0 | OPEN | P1 | | `slug` | a new task',
    ))
    expect(await screen.findByText('Added to the backlog.')).toBeInTheDocument()
    // The board reloads after a successful confirm (initial load + reload).
    await waitFor(() => expect(fetchBacklogTasks).toHaveBeenCalledTimes(2))
  })

  test('surfaces a duplicate id as an editable conflict, not a crash', async () => {
    vi.mocked(draftBacklogTask).mockResolvedValue({
      ok: true,
      line: '- **VOYN-W0-EXAMPLE** | Wave 0 | OPEN | P0 | | `slug` | dup',
      task: {
        task_id: 'VOYN-W0-EXAMPLE',
        wave: '0',
        priority: 'P0',
        status: 'OPEN',
        kind: 'feature',
        title: 'dup',
        body: 'dup',
        repo: null,
      },
    })
    vi.mocked(confirmBacklogTask).mockRejectedValue(
      new Error('POST /api/v1/backlog/intake/confirm -> VOYN-W0-EXAMPLE already exists — chat intake creates new tasks only'),
    )
    render(<Tasks onNavigate={vi.fn()} />)

    fireEvent.change(await screen.findByLabelText('Add or reprioritize by chat'), {
      target: { value: 'dup' },
    })
    fireEvent.click(screen.getByText('Propose'))
    await screen.findByLabelText('Proposed line — edit if needed, then confirm:')
    fireEvent.click(screen.getByText('Confirm'))

    expect(await screen.findByText(/already exists/)).toBeInTheDocument()
  })
})

describe('Tasks — backlog reprioritization (S6d)', () => {
  test('reassigns wave/priority for a backlog task from its row', async () => {
    vi.mocked(reassignBacklogTask).mockResolvedValue({ task_id: 'VOYN-W0-EXAMPLE', reason: 'reassigned', revision: 2 })
    render(<Tasks onNavigate={vi.fn()} />)

    const title = await screen.findByText('Ship the example')
    const row = title.closest('article') as HTMLElement
    fireEvent.click(within(row).getByText('Details'))

    const waveInput = within(row).getByLabelText('Wave')
    fireEvent.change(waveInput, { target: { value: '1' } })
    fireEvent.click(within(row).getByText('Save'))

    await waitFor(() =>
      expect(reassignBacklogTask).toHaveBeenCalledWith('VOYN-W0-EXAMPLE', {
        wave: '1',
        priority: 'P0',
        expected_revision: 1,
      }),
    )
  })

  test('shows the conflict message on a stale revision instead of overwriting', async () => {
    vi.mocked(reassignBacklogTask).mockRejectedValue(
      new Error('POST /api/v1/backlog/tasks/VOYN-W0-EXAMPLE/reassign -> 409'),
    )
    render(<Tasks onNavigate={vi.fn()} />)

    const title = await screen.findByText('Ship the example')
    const row = title.closest('article') as HTMLElement
    fireEvent.click(within(row).getByText('Details'))
    fireEvent.change(within(row).getByLabelText('Wave'), { target: { value: '1' } })
    fireEvent.click(within(row).getByText('Save'))

    expect(await within(row).findByText(/Someone else changed this task first/)).toBeInTheDocument()
  })
})
