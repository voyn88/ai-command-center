import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, test, expect, vi, beforeEach } from 'vitest'
import Home from '../Home'
import { fetchHome } from '../../lib/api'
import type { HomeDTO } from '../../lib/api'

// `fetchHome` is mocked at the module boundary — this test exercises real
// HomeDTO field names/shapes (as defined in lib/api.ts, matched to the real
// backend serializer), not the guessed shape from the original task brief.
vi.mock('../../lib/api', () => ({ fetchHome: vi.fn() }))

const baseHome: HomeDTO = {
  projects: [{ id: 'AICC', name: 'AI Command Center', healthy: true }],
  kpis: {
    projects: { value: 5, meta_key: 'all_healthy', meta_n: 4 },
    agents: { value: 4, meta_key: 'running', meta_n: 2 },
    tasks: { value: 12, meta_key: 'in_progress', meta_n: 0 },
    reviews: { value: 3, meta_key: 'pending', meta_n: 1 },
  },
  queue: [{ title: 'Desktop Architecture D1', project: 'AICC', progress: 63, state: 'RUNNING' }],
  health: { projects_healthy: 4, projects_total: 5 },
  activity: [],
  overview: { reports_count: 3, artifacts_count: 7, recent_activity_count: 2 },
  status: [{ project: 'AICC', repository_state: 'ok' }],
}

const emptyHome: HomeDTO = {
  projects: [],
  kpis: {
    projects: { value: 0, meta_key: 'all_healthy', meta_n: 0 },
    agents: { value: 0, meta_key: 'running', meta_n: 0 },
    tasks: { value: 0, meta_key: 'in_progress', meta_n: 0 },
    reviews: { value: 0, meta_key: 'pending', meta_n: 0 },
  },
  queue: [],
  health: { projects_healthy: 0, projects_total: 0 },
  activity: [],
  overview: { reports_count: 0, artifacts_count: 0, recent_activity_count: 0 },
  status: [],
}

beforeEach(() => {
  vi.mocked(fetchHome).mockReset()
})

describe('Home', () => {
  test('renders real KPI values, a queue row, and the real project-health rollup', async () => {
    vi.mocked(fetchHome).mockResolvedValue(baseHome)
    render(<Home />)

    await waitFor(() => expect(screen.getByText('Desktop Architecture D1')).toBeInTheDocument())
    // Queue row's real `progress` field (63), rendered because it's nonzero.
    expect(screen.getByText('63%')).toBeInTheDocument()
    // A KPI value straight from kpis.tasks.value.
    expect(screen.getByText('12')).toBeInTheDocument()
    // Health donut computed from health.projects_healthy/projects_total
    // (4/5 = 80%) — never the fabricated 94%-style score from the mockup.
    expect(screen.getByText('80%')).toBeInTheDocument()
    // Regression guard: the backend ALWAYS sends the literal meta_key
    // "all_healthy" for the Projects KPI (serializers.py `_kpis`), even
    // when not every project is healthy — baseHome's own fixture is
    // value:5 / meta_n:4 with health 4/5. The KPI card must derive its
    // meta text from the real health rollup, not echo the backend's fixed
    // meta_key, so it must NOT claim "All healthy" here.
    expect(screen.queryByText('All healthy')).not.toBeInTheDocument()
    expect(screen.getByText('4 healthy')).toBeInTheDocument()
  })

  test('redacts sensitive projects: shows "Restricted", no healthy/unhealthy badge or raw metric', async () => {
    vi.mocked(fetchHome).mockResolvedValue({
      ...baseHome,
      projects: [...baseHome.projects, { id: 'BANK', name: 'Bank Strategy', healthy: true, redacted: true }],
    })
    render(<Home />)

    await waitFor(() => expect(screen.getByText('Bank Strategy')).toBeInTheDocument())
    const row = screen.getByText('Bank Strategy').closest('div')
    expect(row).not.toBeNull()
    const scoped = within(row as HTMLElement)
    expect(scoped.getByText('Restricted')).toBeInTheDocument()
    // Must not leak a healthy/unhealthy verdict (or any raw metric) for a
    // redacted project, even though `healthy` is technically still present
    // on the DTO entry.
    expect(scoped.queryByText('Healthy')).not.toBeInTheDocument()
    expect(scoped.queryByText('Needs attention')).not.toBeInTheDocument()
  })

  test('shows tasteful empty states when queue/activity/projects/status are empty', async () => {
    vi.mocked(fetchHome).mockResolvedValue(emptyHome)
    render(<Home />)

    await waitFor(() => expect(screen.getByText('No runs in the execution queue.')).toBeInTheDocument())
    expect(screen.getByText('No recent activity.')).toBeInTheDocument()
  })

  test('shows an error message (not a blank page) when the fetch fails', async () => {
    vi.mocked(fetchHome).mockRejectedValue(new Error('network down'))
    render(<Home />)

    await waitFor(() => expect(screen.getByText('Could not load dashboard data.')).toBeInTheDocument())
  })

  test('has no dead (disabled) controls, no New Task button and no fake sparkline', async () => {
    vi.mocked(fetchHome).mockResolvedValue(baseHome)
    const { container } = render(<Home />)

    await waitFor(() => expect(screen.getByText('Desktop Architecture D1')).toBeInTheDocument())
    expect(container.querySelectorAll('button:disabled')).toHaveLength(0)
    expect(screen.queryByText('New Task')).not.toBeInTheDocument()
    expect(container.querySelector('svg line')).toBeNull()
    expect(screen.getByRole('button', { name: 'Home' })).toHaveAttribute('aria-current', 'page')
  })

  test('queue state is a plain label with the raw id in a tooltip', async () => {
    vi.mocked(fetchHome).mockResolvedValue(baseHome)
    render(<Home />)

    const badge = await screen.findByText('Running now')
    expect(badge).toHaveAttribute('title', 'RUNNING')
    expect(badge).not.toHaveTextContent('RUNNING')
    // raw code is in the collapsed per-panel "Service data" block
    const details = Array.from(document.querySelectorAll('details.tech-details')) as HTMLDetailsElement[]
    expect(details.length).toBeGreaterThanOrEqual(2)
    expect(details.every((d) => !d.open)).toBe(true)
    expect(details[0]).toHaveTextContent('Desktop Architecture D1')
    expect(details[0]).toHaveTextContent('RUNNING')
    fireEvent.click(details[0].querySelector('summary') as HTMLElement)
    expect(details[0].open).toBe(true)
  })

  test('an unknown KPI meta_key renders nothing, never a raw i18n key', async () => {
    vi.mocked(fetchHome).mockResolvedValue({
      ...baseHome,
      kpis: { ...baseHome.kpis, agents: { value: 4, meta_key: 'kpi.agents.meta', meta_n: 2 } },
    })
    render(<Home />)

    await waitFor(() => expect(screen.getByText('Desktop Architecture D1')).toBeInTheDocument())
    expect(screen.queryByText(/kpi\.agents\.meta/)).not.toBeInTheDocument()
    expect(screen.getByText('1 pending')).toBeInTheDocument() // real keys still work
  })
})
