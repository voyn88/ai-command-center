// «Задачи» — the owner's view of the server work queue
// (VOYN-W0-APP-CONTROL-S1, product framing per backlog record AX-S2).
//
// This screen is for a PERSON, not an operator console: one language
// (whatever the app language is — Russian by default for the owner), four
// human statuses («Ожидает / Выполняется / Готово / Не получилось»), and no
// wki_* ids, SHAs or transcripts on the surface — the technical trail lives
// behind «Подробнее», loaded lazily per task so the list stays light.
//
// The owner token is asked for once, inline, when the server answers 401 —
// the SPA is served by the same origin as the API, so there is no separate
// login route to build or protect.

import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import BackgroundSwitcher from '../components/BackgroundSwitcher'
import GlassPanel from '../components/GlassPanel'
import LangToggle from '../components/LangToggle'
import NavItem from '../components/NavItem'
import { ExecutionIcon, HomeIcon, TasksIcon } from '../components/NavIcons'
import { fetchHome } from '../lib/api'
import type { Project } from '../lib/api'
import {
  enqueueAudit,
  fetchQueueItem,
  fetchQueueItems,
  QueueAuthError,
  setOwnerToken,
} from '../lib/queueApi'
import type { QueueItem, QueueItemDetail } from '../lib/queueApi'

const STATUS_ORDER = ['ALL', 'ready', 'claimed', 'succeeded', 'dead'] as const

function statusKey(state: string): string {
  if (state === 'ready') return 'taskStatusReady'
  if (state === 'claimed') return 'taskStatusClaimed'
  if (state === 'succeeded') return 'taskStatusSucceeded'
  if (state === 'dead') return 'taskStatusDead'
  return 'unknownState'
}

function statusTone(state: string): string {
  if (state === 'succeeded') return 'var(--ok)'
  if (state === 'dead') return 'var(--bad)'
  if (state === 'claimed') return 'var(--accent-2)'
  return 'var(--tx3)'
}

function formatDate(value: string | null, language: string, fallback: string) {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

/** A human name for a queue item: the repository or task it concerns —
 * never its wki_* id. */
function taskTitle(item: QueueItem, generic: string): string {
  const subject = item.repository_id || item.task_id
  return subject ? `${generic}: ${subject}` : generic
}

function TaskDetails({ detail, fallback }: { detail: QueueItemDetail; fallback: string }) {
  const { t } = useTranslation()
  const resultText =
    detail.result && typeof detail.result['result_text'] === 'string'
      ? (detail.result['result_text'] as string)
      : null
  return (
    <div className="task-details">
      {detail.dead_reason && (
        <p>
          <span className="task-details-label">{t('taskReason')}</span> {detail.dead_reason}
        </p>
      )}
      {resultText && (
        <p>
          <span className="task-details-label">{t('taskResult')}</span> {resultText}
        </p>
      )}
      <p>
        <span className="task-details-label">{t('taskAttempts')}</span>{' '}
        {detail.attempts.length === 0
          ? fallback
          : detail.attempts
              .map((attempt) => `№${attempt.attempt_no} — ${attempt.state}${attempt.outcome_reason ? ` (${attempt.outcome_reason})` : ''}`)
              .join('; ')}
      </p>
      {/* Технические идентификаторы — только здесь, моноширинно, для копирования в отчёт. */}
      <p className="task-details-tech">{detail.work_item_id}</p>
    </div>
  )
}

function TaskRow({ item, language, fallback }: { item: QueueItem; language: string; fallback: string }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<QueueItemDetail | null>(null)
  const [detailError, setDetailError] = useState(false)

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && !detail) {
      setDetailError(false)
      fetchQueueItem(item.work_item_id).then(setDetail).catch(() => setDetailError(true))
    }
  }

  return (
    <article className="task-row">
      <div className="task-row-head">
        <span className="task-status" style={{ color: statusTone(item.state) }}>
          {t(statusKey(item.state))}
        </span>
        <strong>{taskTitle(item, t('taskGeneric'))}</strong>
        <span className="task-date">{formatDate(item.created_at, language, fallback)}</span>
        <button type="button" className="task-toggle" onClick={toggle}>
          {open ? t('taskHide') : t('taskDetails')}
        </button>
      </div>
      {open && (
        <>
          {!detail && !detailError && <p className="task-details">{t('loading')}</p>}
          {detailError && <p className="task-details" style={{ color: 'var(--bad)' }}>{t('errorLoading')}</p>}
          {detail && <TaskDetails detail={detail} fallback={t('notAvailable')} />}
        </>
      )}
    </article>
  )
}

/** The one-button audit trigger (APP-CONTROL-S4): pick a project, confirm
 * (or edit) the review prompt, and enqueue an `agent_run` — the server
 * pins the safe profile (task_type=review, untrusted=false) and resolves
 * `repository_path` from the project's own config, so the caller here only
 * ever needs a project id. */
function AuditLauncher({ projects, onQueued }: { projects: Project[]; onQueued: () => void }) {
  const { t } = useTranslation()
  const [projectId, setProjectId] = useState(projects[0]?.id ?? '')
  const [prompt, setPrompt] = useState(() => t('auditPromptDefault'))
  const [status, setStatus] = useState<'idle' | 'sending' | 'error' | 'locked'>('idle')
  const [lastQueued, setLastQueued] = useState<string | null>(null)

  useEffect(() => {
    if (!projectId && projects.length > 0) setProjectId(projects[0].id)
  }, [projects, projectId])

  if (projects.length === 0) return null

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!projectId || !prompt.trim() || status === 'sending') return
    setStatus('sending')
    setLastQueued(null)
    try {
      const ack = await enqueueAudit({ project_id: projectId, prompt: prompt.trim() })
      setStatus('idle')
      setLastQueued(ack.work_item_id)
      onQueued()
    } catch (error) {
      setStatus(error instanceof QueueAuthError ? 'locked' : 'error')
    }
  }

  return (
    <GlassPanel title={t('auditTitle')}>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: '.6rem' }}>
        <div style={{ display: 'flex', gap: '.6rem', flexWrap: 'wrap' }}>
          <select
            className="task-token-input"
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
            aria-label={t('auditProjectLabel')}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <button type="submit" className="execution-back" disabled={status === 'sending'}>
            {status === 'sending' ? t('auditSubmitting') : t('auditSubmit')}
          </button>
        </div>
        <textarea
          className="task-token-input"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          aria-label={t('auditPromptLabel')}
          rows={2}
          style={{ width: '100%', resize: 'vertical', fontFamily: 'inherit' }}
        />
        {status === 'error' && <p style={{ color: 'var(--bad)', margin: 0 }}>{t('auditError')}</p>}
        {status === 'locked' && <p style={{ color: 'var(--bad)', margin: 0 }}>{t('auditLocked')}</p>}
        {lastQueued && status === 'idle' && (
          <p style={{ color: 'var(--ok)', margin: 0 }}>{t('auditQueued')}</p>
        )}
      </form>
    </GlassPanel>
  )
}

function TokenGate({ onUnlocked }: { onUnlocked: () => void }) {
  const { t } = useTranslation()
  const [value, setValue] = useState('')
  return (
    <GlassPanel title={t('tokenPrompt')}>
      <p style={{ color: 'var(--tx3)', marginTop: 0 }}>{t('tokenHint')}</p>
      <form
        style={{ display: 'flex', gap: '.6rem', flexWrap: 'wrap' }}
        onSubmit={(event) => {
          event.preventDefault()
          if (!value.trim()) return
          setOwnerToken(value.trim())
          onUnlocked()
        }}
      >
        <input
          type="password"
          className="task-token-input"
          value={value}
          autoComplete="off"
          onChange={(event) => setValue(event.target.value)}
          aria-label={t('tokenPrompt')}
        />
        <button type="submit" className="execution-back">{t('tokenSave')}</button>
      </form>
    </GlassPanel>
  )
}

export default function Tasks({ onNavigate }: { onNavigate: (screen: 'home' | 'execution' | 'tasks') => void }) {
  const { t, i18n } = useTranslation()
  const [items, setItems] = useState<QueueItem[] | null>(null)
  const [locked, setLocked] = useState(false)
  const [error, setError] = useState(false)
  const [filter, setFilter] = useState<string>('ALL')
  const [projects, setProjects] = useState<Project[]>([])

  const load = useCallback(() => {
    setError(false)
    setLocked(false)
    fetchQueueItems(filter)
      .then((data) => setItems(data.items))
      .catch((failure) => {
        if (failure instanceof QueueAuthError) setLocked(true)
        else setError(true)
      })
  }, [filter])
  useEffect(load, [load])
  // The audit launcher's project picker: GET /api/home is unauthenticated
  // (same source Home.tsx uses), independent of the owner-token gate above.
  useEffect(() => {
    fetchHome()
      .then((data) => setProjects(data.projects))
      .catch(() => setProjects([]))
  }, [])

  const visible = items || []

  return (
    <div style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <header className="glass execution-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem' }}>
          <div className="mark" aria-hidden="true" />
          <div><h1>{t('tasksTitle')}</h1><p>{t('tasksSubtitle')}</p></div>
        </div>
        <button type="button" className="execution-back" onClick={() => onNavigate('home')}>{t('backHome')}</button>
      </header>

      <div className="execution-layout">
        <aside className="glass execution-nav">
          <NavItem label={t('home')} icon={<HomeIcon />} onClick={() => onNavigate('home')} />
          <NavItem label={t('execution')} icon={<ExecutionIcon />} onClick={() => onNavigate('execution')} />
          <NavItem label={t('tasks')} icon={<TasksIcon />} active onClick={() => onNavigate('tasks')} />
        </aside>
        <main className="execution-main">
          {locked && <TokenGate onUnlocked={load} />}
          {!locked && !items && !error && <GlassPanel>{t('loading')}</GlassPanel>}
          {!locked && error && (
            <GlassPanel>
              <p style={{ color: 'var(--bad)' }}>{t('errorLoading')}</p>
              <button className="execution-back" onClick={load}>{t('retry')}</button>
            </GlassPanel>
          )}
          {!locked && items && <AuditLauncher projects={projects} onQueued={load} />}
          {!locked && items && (
            <GlassPanel title={t('tasks')}>
              <div className="execution-filters" aria-label={t('allStates')}>
                {STATUS_ORDER.map((state) => (
                  <button
                    key={state}
                    className={filter === state ? 'active' : ''}
                    onClick={() => setFilter(state)}
                  >
                    {state === 'ALL' ? t('allStates') : t(statusKey(state))}
                  </button>
                ))}
              </div>
              <div className="execution-list">
                {visible.length === 0 ? (
                  <p style={{ color: 'var(--tx3)' }}>{t('taskEmpty')}</p>
                ) : (
                  visible.map((item) => (
                    <TaskRow key={item.work_item_id} item={item} language={i18n.language} fallback={t('notAvailable')} />
                  ))
                )}
              </div>
            </GlassPanel>
          )}
        </main>
      </div>
      <footer className="glass execution-footer"><BackgroundSwitcher label={t('background')} /><LangToggle /></footer>
    </div>
  )
}
