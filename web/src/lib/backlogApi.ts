export type BacklogTask = {
  id: string; title: string; status: string; priority: string | null
  project: string; updated_at: string
}
export type BacklogSnapshot = {
  source: 'server_backlog'; read_only: boolean; updated_at: string
  stale: boolean; total: number; tasks: BacklogTask[]; counts: Record<string, number>
  commands_enabled?: boolean
}
export async function fetchBacklog(signal?: AbortSignal): Promise<BacklogSnapshot> {
  const response = await fetch('/api/backlog', { signal, cache: 'no-store' })
  if (!response.ok) throw new Error('Не удалось обновить задачи')
  return response.json()
}
export const statusNames: Record<string, string> = {
  OPEN: 'Запланировано', IN_PROGRESS: 'В работе', READY_TO_REVIEW: 'На проверке',
  DEFER_TO_USER: 'Нужно ваше решение', DONE: 'Завершено', REJECTED: 'Отклонено',
  DECIDED: 'Решение принято', NEEDS_REFINEMENT: 'Нужно уточнить',
  UNTRIAGED: 'Новая задача', SPLIT: 'Разделено на задачи', 'PO-Approved': 'Одобрено',
}
export const statusName = (status: string) => statusNames[status] || status
