// Server data mode of the existing AICC React application. Reuses its visual
// components; the local workspace and privileged queue client stay unchanged.
import { useEffect, useState } from 'react'
import GlassPanel from '../components/GlassPanel'
import KpiCard from '../components/KpiCard'
import NavItem from '../components/NavItem'
import OwnerTaskActions from '../components/OwnerTaskActions'
import { HomeIcon, TasksIcon, ExecutionIcon } from '../components/NavIcons'
import { fetchBacklog, statusName } from '../lib/backlogApi'
import type { BacklogSnapshot, BacklogTask } from '../lib/backlogApi'
import '../theme/serverWorkspace.css'

type Screen = 'home' | 'tasks' | 'execution'
const initialScreen = (): Screen => window.location.hash === '#tasks' || window.location.search.includes('master_backlog') ? 'tasks' : window.location.hash === '#execution' ? 'execution' : 'home'
const priorities: Record<string, string> = { P0: 'Критический', P1: 'Высокий', P2: 'Обычный', P3: 'Низкий' }
const order: Record<string, number> = { DEFER_TO_USER: 0, IN_PROGRESS: 1, READY_TO_REVIEW: 2, NEEDS_REFINEMENT: 3, OPEN: 4 }
const displayDate = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Дата неизвестна' : date.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

function TaskCard({ task, commands, onChanged }: { task: BacklogTask; commands?: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  return <article className="owner-task">
    <div className="owner-task-meta"><span className={`owner-status status-${task.status}`}>{statusName(task.status)}</span><span>{task.project}</span></div>
    <h3>{task.title === '[REDACTED]' ? 'Описание скрыто: содержит служебные данные' : task.title}</h3>
    <div className="owner-task-meta"><span>{task.priority ? `${task.priority} · ${priorities[task.priority]}` : 'Приоритет не задан'}</span><span>{displayDate(task.updated_at)}</span></div>
    <details onToggle={e => setOpen(e.currentTarget.open)}><summary>Подробнее{commands ? ' и действия' : ''}</summary><p className="owner-id">{task.id}</p><p>Статус из серверного списка задач. Завершение задачи само по себе не подтверждает её развёртывание.</p>{commands && open && <OwnerTaskActions taskId={task.id} onChanged={onChanged} />}</details>
  </article>
}

export default function ServerWorkspace() {
  const [screen, setScreen] = useState<Screen>(initialScreen)
  const [data, setData] = useState<BacklogSnapshot | null>(null)
  const [error, setError] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('ALL')
  const [project, setProject] = useState('ALL')
  const [limit, setLimit] = useState(30)

  useEffect(() => {
    document.documentElement.lang = 'ru'
    const update = () => setScreen(initialScreen())
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    let busy = false
    const load = async () => {
      if (busy || controller.signal.aborted) return
      busy = true
      try {
        const next = await fetchBacklog(controller.signal)
        if (!controller.signal.aborted) { setData(next); setError(false) }
      } catch { if (!controller.signal.aborted) setError(true) }
      finally { busy = false; if (!controller.signal.aborted) setLoading(false) }
    }
    void load()
    const timer = window.setInterval(() => { if (!document.hidden) void load() }, 60000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [refresh])

  const navigate = (next: Screen, nextStatus = 'ALL') => {
    setScreen(next); setStatus(nextStatus); setLimit(30)
    window.history.replaceState(null, '', window.location.pathname + '#' + next)
  }
  const tasks = data?.tasks || []
  const counts = data?.counts || {}
  const filtered = tasks.filter(task =>
    (status === 'ALL' || task.status === status) &&
    (project === 'ALL' || task.project === project) &&
    (screen !== 'execution' || ['IN_PROGRESS', 'READY_TO_REVIEW'].includes(task.status)) &&
    `${task.title} ${task.id} ${task.project}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
  ).sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || (a.priority || 'Z').localeCompare(b.priority || 'Z') || b.updated_at.localeCompare(a.updated_at))
  const attention = tasks.filter(t => t.status === 'DEFER_TO_USER')
  return <div className="owner-shell">
    <header className="glass owner-header"><div className="owner-brand"><div className="mark" aria-hidden="true" /><div><strong>AI Command Center</strong><p>Разработка под контролем</p></div></div><button className="owner-button" onClick={() => { setLoading(true); setRefresh(n => n + 1) }} disabled={loading}>{loading ? 'Обновление…' : 'Обновить'}</button></header>
    <nav className="glass owner-nav" aria-label="Главное меню">
      <NavItem label="Обзор" icon={<HomeIcon />} active={screen === 'home'} onClick={() => navigate('home')} />
      <NavItem label="Задачи" icon={<TasksIcon />} active={screen === 'tasks'} onClick={() => navigate('tasks')} />
      <NavItem label="В работе" icon={<ExecutionIcon />} active={screen === 'execution'} onClick={() => navigate('execution')} />
    </nav>
    <main>
      {error && <div role="alert" className="owner-notice">Не удалось обновить данные. {data ? 'Показан последний полученный список.' : 'Попробуйте обновить страницу.'}</div>}
      {data?.stale && <div role="alert" className="owner-notice">Список давно не обновлялся. Статусы могут быть неактуальны.</div>}
      {!data && !error && <p role="status">Загружаем серверные задачи…</p>}
      {data && <>
        <div className="owner-page-heading"><h1>{screen === 'home' ? 'Обзор разработки' : screen === 'tasks' ? 'Задачи' : 'В работе и на проверке'}</h1><p>Обновлено {displayDate(data.updated_at)} · {data.commands_enabled ? 'Действия доступны в карточках' : 'Только просмотр'}</p></div>
        {screen === 'home' ? <>
          <div className="owner-kpis">
            {[['IN_PROGRESS', 'В работе'], ['READY_TO_REVIEW', 'На проверке'], ['DEFER_TO_USER', 'Ждут вас'], ['DONE', 'Завершено']].map(([key, label]) => <button key={key} className="owner-kpi-button" onClick={() => navigate('tasks', key)} aria-label={`${label}: ${counts[key] || 0}`}><KpiCard label={label} value={counts[key] || 0} meta="Открыть задачи" /></button>)}
          </div>
          <GlassPanel title="Нужно ваше решение">
            <p className="owner-muted">{attention.length ? `${attention.length} задач ждут ответа. ${data.commands_enabled ? 'Откройте карточку, чтобы отправить решение.' : 'Доступен только просмотр.'}` : 'Сейчас нет задач, ожидающих вашего решения.'}</p>
            {attention.slice(0, 4).map((task, i) => <TaskCard key={`${task.id}-${i}`} task={task} commands={data.commands_enabled} onChanged={() => setRefresh(n => n + 1)} />)}
            {attention.length > 4 && <button className="owner-button" onClick={() => navigate('tasks', 'DEFER_TO_USER')}>Все ожидающие решения ({attention.length})</button>}
          </GlassPanel>
          <div className="owner-section"><GlassPanel title="Проекты">
            {[...new Set(tasks.map(t => t.project))].sort().map(name => <button className="owner-project" key={name} onClick={() => { setProject(name); navigate('tasks') }}><strong>{name}</strong><span>{tasks.filter(t => t.project === name).length} задач →</span></button>)}
          </GlassPanel></div>
        </> : <GlassPanel>
          <div className="owner-filters">
            <label>Поиск<input value={query} placeholder="Название или номер задачи" onChange={e => { setQuery(e.target.value); setLimit(30) }} /></label>
            <label>Проект<select value={project} onChange={e => { setProject(e.target.value); setLimit(30) }}><option value="ALL">Все проекты</option>{[...new Set(tasks.map(t => t.project))].sort().map(name => <option key={name}>{name}</option>)}</select></label>
            <label>Статус<select value={status} onChange={e => { setStatus(e.target.value); setLimit(30) }}><option value="ALL">Все статусы</option>{Object.keys(counts).filter(s => screen !== 'execution' || ['IN_PROGRESS', 'READY_TO_REVIEW'].includes(s)).map(s => <option value={s} key={s}>{statusName(s)} ({counts[s]})</option>)}</select></label>
          </div>
          <p className="owner-muted" role="status">Найдено {filtered.length} · Всего {data.total}</p>
          {filtered.slice(0, limit).map((task, i) => <TaskCard key={`${task.id}-${i}`} task={task} commands={data.commands_enabled} onChanged={() => setRefresh(n => n + 1)} />)}
          {!filtered.length && <p>По этим фильтрам задач нет.</p>}
          {filtered.length > limit && <button className="owner-button" onClick={() => setLimit(n => n + 30)}>Показать ещё ({filtered.length - limit})</button>}
        </GlassPanel>}
      </>}
    </main>
    <footer className="owner-footer">AICC · Серверные задачи · Обновление каждую минуту</footer>
  </div>
}
