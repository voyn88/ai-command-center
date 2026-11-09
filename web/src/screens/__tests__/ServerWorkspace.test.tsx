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
  test('stale badge shows the idle days on an in-progress card', async () => {
    const old = new Date(Date.now() - 5 * 24 * 3600 * 1000).toISOString()
    vi.mocked(fetchBacklog).mockResolvedValue({
      ...snapshot,
      tasks: [{ id: 'one', title: 'Deploy web', status: 'IN_PROGRESS', project: 'AICC', priority: 'P1', updated_at: old }],
    })
    render(<ServerWorkspace />)
    await screen.findByText('Дольше обычного без движения: 1')
    expect(screen.getByText('Без движения 5 дн.')).toBeInTheDocument()
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
  const ago = (d: number, extraHours = 1) => new Date(Date.now() - (d * 24 + extraHours) * 3600 * 1000).toISOString()
  const mk = (id: string, status: string, d: number, priority: string | null = null) => ({ id, title: `T ${id}`, status, project: 'AICC', priority, updated_at: ago(d) })

  test('only statuses that should move are stalled, at their own thresholds', async () => {
    vi.mocked(fetchBacklog).mockResolvedValue({
      ...snapshot,
      tasks: [
        mk('open30', 'OPEN', 30), mk('untri30', 'UNTRIAGED', 30), mk('defer30', 'DEFER_TO_USER', 30),
        mk('ip3', 'IN_PROGRESS', 3), mk('ip2', 'IN_PROGRESS', 2), mk('rr2', 'READY_TO_REVIEW', 2),
        mk('nr4', 'NEEDS_REFINEMENT', 4), mk('nr5', 'NEEDS_REFINEMENT', 5), mk('done', 'DONE', 30),
      ],
    })
    render(<ServerWorkspace />)
    await screen.findByText('Дольше обычного без движения: 3')
    const stalePanel = screen.getByRole('heading', { name: 'Давно без движения' }).closest('section') as HTMLElement
    const ids = Array.from(stalePanel.querySelectorAll('h3')).map(h => h.textContent)
    expect(ids.sort()).toEqual(['T ip3', 'T nr5', 'T rr2'])
    // the owner-decision card is in its own block only, with no stalled badge
    const decision = screen.getByRole('heading', { name: 'Нужно ваше решение' }).closest('section') as HTMLElement
    expect(decision).toHaveTextContent('T defer30')
    expect(decision).not.toHaveTextContent('Без движения')
    expect(stalePanel).not.toHaveTextContent('T defer30')
  })

  test('cap of 5, "Показать все (7)" opens the STALLED filter with exactly 7 rows; no card in two blocks', async () => {
    const stalled = Array.from({ length: 7 }, (_, i) => mk(`s${i}`, 'IN_PROGRESS', 4 + i, i % 2 ? 'P1' : 'P0'))
    vi.mocked(fetchBacklog).mockResolvedValue({ ...snapshot, tasks: [...stalled, mk('d1', 'DEFER_TO_USER', 9)], counts: { IN_PROGRESS: 7, DEFER_TO_USER: 1 }, total: 8 })
    render(<ServerWorkspace />)
    await screen.findByText('Дольше обычного без движения: 7')
    const stalePanel = screen.getByRole('heading', { name: 'Давно без движения' }).closest('section') as HTMLElement
    const decision = screen.getByRole('heading', { name: 'Нужно ваше решение' }).closest('section') as HTMLElement
    const idsOf = (el: HTMLElement) => Array.from(el.querySelectorAll('h3')).map(h => h.textContent)
    expect(idsOf(stalePanel)).toHaveLength(5)
    expect(idsOf(stalePanel)[0]).toBe('T s6') // the longest overshoot first
    expect(idsOf(stalePanel).filter(id => idsOf(decision).includes(id))).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Показать все (7)' }))
    const option = await screen.findByRole('option', { name: 'Давно без движения (7)' })
    expect((option as HTMLOptionElement).selected).toBe(true)
    expect(screen.getAllByRole('article')).toHaveLength(7)
    // "Сбросить фильтры" clears the virtual status
    fireEvent.change(screen.getByLabelText('Поиск'), { target: { value: 'zzz' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Сбросить фильтры' }))
    expect(screen.getAllByRole('article').length).toBe(8)
  })

  test('"Показать все" resets project/search filters so the row count equals N', async () => {
    const mkp = (id: string, project: string) => ({ ...mk(id, 'IN_PROGRESS', 5 + Number(id.slice(1))), project })
    vi.mocked(fetchBacklog).mockResolvedValue({
      ...snapshot,
      tasks: [...Array.from({ length: 6 }, (_, i) => mkp(`s${i}`, 'AICC')), mkp('s6', 'AIOS')],
      counts: { IN_PROGRESS: 7 }, total: 7,
    })
    render(<ServerWorkspace />)
    await screen.findByText('Дольше обычного без движения: 7')
    // a project filter is left over from browsing the projects list
    fireEvent.click(screen.getByRole('button', { name: /AIOS/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Обзор' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Показать все (7)' }))
    expect(screen.getAllByRole('article')).toHaveLength(7)
  })

  test('service data is a collapsed native disclosure with the raw codes', async () => {
    vi.mocked(fetchBacklog).mockResolvedValue({ ...snapshot, tasks: [{ id: 'x1', title: 'Odd', status: 'WEIRD', project: 'AICC', priority: 'P0', updated_at: '-' }] })
    window.location.hash = '#tasks'
    render(<ServerWorkspace />)
    await screen.findByText('Odd')
    const details = document.querySelector('details.tech-details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    const summary = details.querySelector('summary') as HTMLElement
    expect(summary).toHaveTextContent('Служебные данные')
    expect(summary).not.toHaveAttribute('tabindex', '-1')
    fireEvent.click(summary)
    expect(details.open).toBe(true)
    expect(details).toHaveTextContent('WEIRD')
    expect(details).toHaveTextContent('x1')
  })
  test('unknown backlog status is a neutral phrase with the raw code in a tooltip', async () => {
    vi.mocked(fetchBacklog).mockResolvedValue({ ...snapshot, tasks: [{ id: 'x', title: 'Odd', status: 'SOMETHING_NEW', project: 'AICC', priority: null, updated_at: '-' }] })
    window.location.hash = '#tasks'
    render(<ServerWorkspace />)
    const badge = await screen.findByText('Статус уточняется')
    expect(badge).toHaveAttribute('title', 'SOMETHING_NEW')
  })
  test('no reset button when no filter is active; no focus loss after retry', async () => {
    window.location.hash = '#tasks'
    vi.mocked(fetchBacklog).mockResolvedValue({ ...snapshot, tasks: [], total: 0, counts: {} })
    render(<ServerWorkspace />)
    expect(await screen.findByText('Задач пока нет.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Сбросить фильтры' })).not.toBeInTheDocument()
  })
  test('focus moves to the refresh button after a successful retry', async () => {
    vi.mocked(fetchBacklog).mockRejectedValueOnce(new Error('offline'))
    render(<ServerWorkspace />)
    const retry = await screen.findByRole('button', { name: 'Повторить' })
    retry.focus()
    fireEvent.click(retry)
    await screen.findByText('Choose scope')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Обновить' })).toHaveFocus())
  })
})
