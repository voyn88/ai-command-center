import { useEffect, useState } from 'react'

type TaskInfo = { revision: number; status: string; body: string; can_launch: boolean; can_decide: boolean }
type Intent = { task_id: string; expected_revision: number; idempotency_key: string; device_id: string; action: 'launch' | 'decision'; decision?: string; note?: string }
const reasons: Record<string, string> = {
  queued: 'Задача поставлена в очередь. Агент начнёт работу, когда освободится место.',
  OPEN: 'Решение сохранено. Задача возвращена в план разработки.',
  REJECTED: 'Решение сохранено. Задача отклонена.',
  NEEDS_REFINEMENT: 'Решение сохранено. Задача ожидает уточнения.',
  revision_conflict: 'Задача изменилась. Закройте подробности и откройте их снова.',
  idempotency_conflict: 'Повторный запрос отличается от исходного. Обновите страницу.',
  wip_exhausted: 'Достигнут лимит параллельной работы. Попробуйте позже.',
  repo_busy: 'В этом проекте уже выполняется работа. Дождитесь её завершения.',
  dependencies_not_done: 'Сначала нужно завершить зависимые задачи.',
  earlier_wave_has_eligible_work: 'Сначала должны пройти задачи предыдущего этапа.',
  planner_busy: 'Планировщик занят. Попробуйте позже.',
  review_backlog_full: 'Накопились задачи на проверке. Запуск временно отложен.',
  not_eligible: 'Задача сейчас недоступна для запуска. Возможно, она уже запущена или ждёт зависимостей.',
  not_awaiting_decision: 'Задача больше не ожидает решения.',
  forbidden: 'Для этого действия нет прав.',
  invalid_note: 'Напишите решение или пояснение.',
  unknown_task: 'Задача не найдена.',
}

function deviceId() {
  let id = sessionStorage.getItem('aicc-owner-device')
  if (!id) { id = crypto.randomUUID(); sessionStorage.setItem('aicc-owner-device', id) }
  return id
}

export default function OwnerTaskActions({ taskId, onChanged }: { taskId: string; onChanged: () => void }) {
  const [info, setInfo] = useState<TaskInfo | null>(null)
  const [message, setMessage] = useState('')
  const [note, setNote] = useState('')
  const [decision, setDecision] = useState('resume')
  const [busy, setBusy] = useState(false)
  const [pending, setPending] = useState<Intent | null>(null)
  const [done, setDone] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/owner/task?task_id=' + encodeURIComponent(taskId), { signal: controller.signal, cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error(); return r.json() })
      .then(setInfo).catch(() => { if (!controller.signal.aborted) setMessage('Не удалось загрузить действия. Закройте подробности и попробуйте снова.') })
    return () => controller.abort()
  }, [taskId])
  const send = async (action: Intent['action']) => {
    if (!info || busy) return
    const intent = pending || { task_id: taskId, expected_revision: info.revision, idempotency_key: crypto.randomUUID(), device_id: deviceId(), action, ...(action === 'decision' ? { decision, note } : {}) }
    setPending(intent); setBusy(true); setMessage('')
    try {
      const response = await fetch('/api/owner/commands', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-AICC-Request': 'owner-command-v1' }, body: JSON.stringify(intent) })
      if (!response.ok) throw new Error()
      const result = await response.json()
      setMessage(reasons[result.reason] || (result.ok ? 'Действие выполнено.' : 'Действие не выполнено. Обновите задачу и попробуйте позже.'))
      setPending(null)
      if (result.ok) { setDone(true); onChanged() }
    } catch { setMessage('Нет подтверждения сервера. Повторите отправку: повторный запрос не создаст второе действие.') }
    finally { setBusy(false) }
  }
  return <div className="owner-actions">
    {info?.body && info.body !== '[REDACTED]' && <p style={{ whiteSpace: 'pre-wrap' }}>{info.body}</p>}
    {!info && !message && <p>Загружаем действия…</p>}
    {!done && info?.can_decide && <>
      <label>Решение<select value={decision} disabled={busy || !!pending} onChange={e => setDecision(e.target.value)}><option value="resume">Продолжить работу</option><option value="refine">Отправить на уточнение</option><option value="reject">Отклонить задачу</option></select></label>
      <label>Ваш ответ<textarea value={note} maxLength={4000} disabled={busy || !!pending} onChange={e => setNote(e.target.value)} placeholder="Что нужно сделать или почему принято это решение" /></label>
      <button className="owner-button" disabled={busy || !note.trim()} onClick={() => void send('decision')}>{busy ? 'Отправляем…' : pending ? 'Повторить отправку' : 'Отправить решение'}</button>
    </>}
    {!done && info?.can_launch && <button className="owner-button" disabled={busy} onClick={() => void send('launch')}>{busy ? 'Отправляем…' : pending ? 'Повторить отправку' : 'Запустить агента по задаче'}</button>}
    {info && !info.can_launch && !info.can_decide && !done && <p>Для текущего статуса действий нет.</p>}
    {message && <p role="status">{message}</p>}
  </div>
}
