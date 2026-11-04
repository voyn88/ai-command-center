import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import DeliveryTeamPanel from './DeliveryTeamPanel'
import { fetchDeliveryTeam } from '../lib/deliveryTeamApi'
import type { DeliveryTeamSnapshot } from '../lib/deliveryTeamApi'

vi.mock('../lib/deliveryTeamApi', () => ({ fetchDeliveryTeam: vi.fn() }))
const snapshot: DeliveryTeamSnapshot = {
  source: 'canonical_postgresql', read_only: true, enabled: true, stopped: false,
  available: true, reason: null, updated_at: '2026-11-04T00:00:00Z', limit: 30, offset: 0,
  total: 42, counts: { blocked: 2, production_accepted: 1, sha_recorded: 7, merged: null },
  roles: [{ stage: 'architect', name: 'Главный архитектор' }],
  tasks: [{ id: 'task-1', title: 'Проверить контракт', project: 'AICC', revision: 4,
    backlog_status: 'DONE', merged: null, production_accepted: false,
    assignments: [{ stage: 'architect', role: 'Главный архитектор', status: 'blocked',
      blocker: 'Нужна совместимость', next_action: 'Уточнить контракт', due_at: null, updated_at: '-' },
    { stage: 'ux', role: 'UX/UI', status: 'waived', blocker: null, next_action: '-', due_at: null, updated_at: '-' }],
    corrections: [{ task_id: 'VOYN-FIX-1', owner: 'developer', action: 'Исправить фикстуру',
      due_at: '2026-11-01T00:00:00Z', assigned_by: 'cto_policy', effectiveness_checked: false, overdue: true, escalation_task_id: 'VOYN-FIX-1-OVERDUE-1' }] }],
}
beforeEach(() => { vi.mocked(fetchDeliveryTeam).mockReset().mockResolvedValue(snapshot) })
describe('native delivery team', () => {
  test('shows actual assignments and separates acceptance from DONE', async () => {
    render(<DeliveryTeamPanel />)
    await screen.findByText('Проверить контракт')
    expect(screen.getByText('Production: приёмка не подтверждена')).toBeInTheDocument()
    expect(screen.getByText('Главный архитектор · Заблокировано')).toBeInTheDocument()
    expect(screen.getByText('Блокер: Нужна совместимость')).toBeInTheDocument()
    expect(screen.getAllByText('Срок не назначен')).toHaveLength(2)
    expect(screen.getByText('UX/UI · Не требуется по оценке архитектора')).toBeInTheDocument()
    expect(screen.getByText('Корректирующие действия (1, есть просроченные)')).toBeInTheDocument()
    expect(screen.getByText('Назначил: политика CTO')).toBeInTheDocument()
    expect(screen.getByText('Эффективность не подтверждена')).toBeInTheDocument()
    expect(screen.getByText('Эскалировано CTO: VOYN-FIX-1-OVERDUE-1')).toBeInTheDocument()
    expect(screen.getByText('Показано 1 из 42, начиная с 1.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Следующие' }))
    await waitFor(() => expect(fetchDeliveryTeam).toHaveBeenLastCalledWith(30, expect.any(AbortSignal)))
  })
  test('missing ledger does not show zero green counters', async () => {
    vi.mocked(fetchDeliveryTeam).mockResolvedValue({ ...snapshot, enabled: false, stopped: true,
      available: false, reason: 'ledger_unavailable', total: null, counts: null, tasks: [] })
    render(<DeliveryTeamPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('приёмка не подтверждены')
    expect(screen.getByText('Команда: не включена · Выполнение: остановлено')).toBeInTheDocument()
    expect(screen.queryByText('Production принят:')).not.toBeInTheDocument()
  })
  test('network failure does not retain an apparent current verdict', async () => {
    vi.mocked(fetchDeliveryTeam).mockRejectedValue(new Error('offline'))
    render(<DeliveryTeamPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Статусы не подтверждены')
    expect(screen.queryByText('Проверить контракт')).not.toBeInTheDocument()
  })
})
