import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import ServerWorkspace from '../ServerWorkspace'
import { fetchBacklog } from '../../lib/backlogApi'
import type { BacklogSnapshot } from '../../lib/backlogApi'

vi.mock('../../lib/backlogApi', async (original) => ({ ...await original<object>(), fetchBacklog: vi.fn() }))
const snapshot: BacklogSnapshot = {
  source: 'server_backlog', read_only: true, updated_at: '2026-10-31T00:00:00Z', stale: false, total: 3,
  counts: { IN_PROGRESS: 1, DEFER_TO_USER: 1, DONE: 1 },
  tasks: [
    { id: 'one', title: 'Deploy web', status: 'IN_PROGRESS', project: 'AICC', priority: 'P1', updated_at: '-' },
    { id: 'two', title: 'Choose scope', status: 'DEFER_TO_USER', project: 'AIOS', priority: 'P0', updated_at: '-' },
    { id: 'three', title: 'Old work', status: 'DONE', project: 'AICC', priority: null, updated_at: '-' },
  ],
}
beforeEach(() => {
  window.history.replaceState(null, '', '/')
  vi.mocked(fetchBacklog).mockReset().mockResolvedValue(snapshot)
})
describe('server workspace', () => {
  test('shows server counts and opens filtered tasks', async () => {
    render(<ServerWorkspace />)
    await screen.findByText('Choose scope')
    fireEvent.click(screen.getByRole('button', { name: 'В работе: 1' }))
    expect(await screen.findByText('Deploy web')).toBeInTheDocument()
    expect(screen.queryByText('Old work')).not.toBeInTheDocument()
    expect(screen.queryByText('Choose scope')).not.toBeInTheDocument()
  })
  test('searches tasks and preserves explicit empty states', async () => {
    window.location.hash = '#tasks'
    render(<ServerWorkspace />)
    await screen.findByText('Old work')
    fireEvent.change(screen.getByLabelText('Поиск'), { target: { value: 'does not exist' } })
    expect(screen.getByText('По этим фильтрам задач нет.')).toBeInTheDocument()
  })
  test('reports stale snapshots and refresh failures without erasing data', async () => {
    vi.mocked(fetchBacklog).mockResolvedValueOnce({ ...snapshot, stale: true }).mockRejectedValue(new Error('offline'))
    render(<ServerWorkspace />)
    await screen.findByText(/Список давно не обновлялся/)
    fireEvent.click(screen.getByRole('button', { name: 'Обновить' }))
    await waitFor(() => expect(screen.getByText(/Не удалось обновить данные/)).toBeInTheDocument())
    expect(screen.getByText('Choose scope')).toBeInTheDocument()
  })
})
