import { useEffect, useState } from 'react'
import GlassPanel from './GlassPanel'
import { fetchDeliveryTeam } from '../lib/deliveryTeamApi'
import type { DeliveryTeamSnapshot } from '../lib/deliveryTeamApi'

const states: Record<string, string> = { pending: 'Ожидает', queued: 'Назначено', passed: 'Этап пройден', blocked: 'Заблокировано', stale: 'Требует повторной оценки' }
const date = (value: string) => new Date(value).toLocaleString('ru-RU')

export default function DeliveryTeamPanel({ refresh = 0 }: { refresh?: number }) {
  const [data, setData] = useState<DeliveryTeamSnapshot | null>(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [offset, setOffset] = useState(0)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setData(null)
    setError(false)
    fetchDeliveryTeam(offset, controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value)
    }).catch(() => { if (!controller.signal.aborted) setError(true) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [offset, refresh, retry])
  return <GlassPanel title="Команда разработки" action={<button className="owner-button" disabled={loading} onClick={() => setRetry(n => n + 1)}>Обновить команду</button>}>
    {loading && <p role="status">Загружаем назначения команды…</p>}
    {error && <p role="alert">Данные команды временно недоступны. Статусы не подтверждены.</p>}
    {data && <>
      <p>Источник: канонический серверный реестр · {date(data.updated_at)} · Только просмотр</p>
      <p>Команда: {data.enabled ? 'включена' : 'не включена'} · Выполнение: {data.stopped ? 'остановлено' : 'стоп не установлен'}</p>
      <details><summary>Роли команды ({data.roles.length})</summary><p>{data.roles.map(r => r.name).join(' · ')}</p><p>Наличие роли в настройках не означает, что проверка выполнена.</p></details>
      {!data.available ? <p role="alert">{data.reason === 'database_unavailable' ? 'Нет подключения к серверному реестру.' : 'Реестр назначений команды пока недоступен.'} Выполнение задач и приёмка не подтверждены.</p> : <>
        {!data.enabled && <p role="alert">Обработка командой не включена. Показаны только сохранённые назначения.</p>}
        {data.stopped && <p role="alert">Обработка командой остановлена. Сохранённые результаты доступны для просмотра.</p>}
        <div className="owner-kpis">
          <p>Задач в текущем реестре: <strong>{data.total}</strong></p>
          <p>Заблокировано: <strong>{data.counts?.blocked}</strong></p>
          <p>SHA в реестре: <strong>{data.counts?.sha_recorded}</strong></p>
          <p>Production принят: <strong>{data.counts?.production_accepted}</strong></p>
        </div>
        <p>Записанный SHA и пройденное ревью не подтверждают слияние или приёмку production. Слияние: отдельного подтверждения в этом реестре нет.</p>
        <p role="status">Показано {data.tasks.length} из {data.total}, начиная с {data.tasks.length ? data.offset + 1 : 0}.</p>
        {!data.tasks.length && <p>В этой части реестра назначений нет. Это не означает, что весь бэклог обработан.</p>}
        {data.tasks.map(task => <article className="owner-task" key={task.id}>
          <div className="owner-task-meta"><span>{task.project}</span><span>Ревизия {task.revision}</span></div>
          <h3>{task.title === '[REDACTED]' ? 'Описание скрыто' : task.title}</h3>
          <p className="owner-id">{task.id}</p>
          <p>Production: {task.production_accepted ? 'принят по подтверждённым критериям' : 'приёмка не подтверждена'}</p>
          <details><summary>Назначения и следующие действия ({task.assignments.length})</summary>
            {task.assignments.map(assignment => <div className="owner-task" key={assignment.stage}>
              <strong>{assignment.role} · {states[assignment.status] || assignment.status}</strong>
              {assignment.blocker && <p>Блокер: {assignment.blocker === '[REDACTED]' ? 'Подробности скрыты' : assignment.blocker}</p>}
              <p>Следующее действие: {assignment.next_action}</p>
              <p>{assignment.due_at ? `Срок: ${date(assignment.due_at)}` : 'Срок не назначен'}</p>
            </div>)}
          </details>
        </article>)}
        <div className="owner-filters">
          <button className="owner-button" disabled={loading || offset === 0} onClick={() => setOffset(n => Math.max(0, n - 30))}>Предыдущие</button>
          <button className="owner-button" disabled={loading || data.offset + data.tasks.length >= (data.total ?? 0)} onClick={() => setOffset(n => n + 30)}>Следующие</button>
        </div>
      </>}
    </>}
  </GlassPanel>
}
