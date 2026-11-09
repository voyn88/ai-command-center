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
  test('marks in-progress tasks idle for 3+ days as stale', async () => {
    const old = new Date(Date.now() - 5 * 24 * 3600 * 1000).toISOString()
    vi.mocked(fetchBacklog).mockResolvedValue({
      ...snapshot,
      tasks: [{ id: 'one', title: 'Deploy web', status: 'IN_PROGRESS', project: 'AICC', priority: 'P1', updated_at: old }],
    })
    render(<ServerWorkspace />)
    await screen.findByText('Без движения 3 дня и больше: 1')
    expect(screen.getAllByText('Давно без движения').length).toBeGreaterThan(1)
    expect(screen.getByText('обновлено 5 дн. назад')).toBeInTheDocument()
  })
  test('empty filter result offers to reset the filters', async () => {
    window.location.hash = '#tasks'
    render(<ServerWorkspace />)
    await screen.findByText('Old work')
    fireEvent.change(screen.getByLabelText('Поиск'), { target: { value: 'does not exist' } })
    fireEvent.click(screen.getByRole('button', { name: 'Сбросить фильтры' }))
    expect(await screen.findByText('Old work')).toBeInTheDocument()
  })
  test('failed first load shows a retry button, not a dead end', async () => {
    vi.mocked(fetchBacklog).mockRejectedValueOnce(new Error('offline'))
    render(<ServerWorkspace />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Не удалось загрузить задачи. Проверьте соединение и нажмите «Повторить».')
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }))
    expect(await screen.findByText('Choose scope')).toBeInTheDocument()
  })
  test('shows the loading skeleton while the first request is pending', async () => {
    vi.mocked(fetchBacklog).mockReturnValue(new Promise(() => {}))
    render(<ServerWorkspace />)
    expect(screen.getByRole('status', { name: 'Загружаем задачи…' })).toBeInTheDocument()
  })
  test('decision block comes before the status tiles', async () => {
    render(<ServerWorkspace />)
    await screen.findByText('Choose scope')
    const heading = screen.getAllByText('Нужно ваше решение')[0]
    const tile = screen.getByRole('button', { name: 'В работе: 1' })
    expect(heading.compareDocumentPosition(tile) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
