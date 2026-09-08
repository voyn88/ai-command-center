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
  confirmBacklogTask,
  draftBacklogTask,
  fetchBacklogStatus,
  fetchBacklogTaskDetail,
  fetchBacklogTasks,
  reassignBacklogTask,
} from '../lib/backlogApi'
import type { BacklogStatusCounts, BacklogTask, BacklogTaskDetail, DraftResult } from '../lib/backlogApi'
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

const BACKLOG_STATUSES = [
  'OPEN',
  'IN_PROGRESS',
  'READY_TO_REVIEW',
  'DONE',
  'UNTRIAGED',
  'DEFER_TO_USER',
  'SPLIT',
  'NEEDS_REFINEMENT',
  'DECIDED',
] as const

function backlogStatusLabel(status: string, t: (key: string) => string): string {
  const key = `backlogStatus${status}`
  const label = t(key)
  return label === key ? status : label
}

function backlogStatusTone(status: string): string {
  if (status === 'DONE') return 'var(--ok)'
  if (status === 'DEFER_TO_USER' || status === 'NEEDS_REFINEMENT') return 'var(--bad)'
  if (status === 'IN_PROGRESS' || status === 'READY_TO_REVIEW') return 'var(--accent-2)'
  return 'var(--tx3)'
}

/** History for one backlog task (S6c drill-down): the transition trail that
 * makes up its "decomposition" — this record IS one piece of the larger
 * central task, and its events are the progress record for that piece. */
function BacklogHistory({ taskId }: { taskId: string }) {
  const { t } = useTranslation()
  const [detail, setDetail] = useState<BacklogTaskDetail | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    setDetail(null)
    setFailed(false)
    fetchBacklogTaskDetail(taskId).then(setDetail).catch(() => setFailed(true))
  }, [taskId])

  if (failed) return <p className="task-details" style={{ color: 'var(--bad)' }}>{t('errorLoading')}</p>
  if (!detail) return <p className="task-details">{t('loading')}</p>
  return (
    <div className="task-details">
      {detail.events.length === 0 ? (
        <p>{t('backlogHistoryEmpty')}</p>
      ) : (
        detail.events.map((event, index) => (
          <p key={`${event.created_at}-${index}`}>
            {event.event} — {event.outcome}
            {event.reason ? ` (${event.reason})` : ''}
          </p>
        ))
      )}
    </div>
  )
}

/** Inline priority/wave reprioritization for one backlog task (S6d): the
 * optimistic-revision form the same operation the chat/voice parser drives
 * (S6a/S6b) when it re-issues a task line with a changed token. */
function BacklogReassignForm({
  item,
  onSaved,
}: {
  item: BacklogTask
  onSaved: (next: { wave: string; priority: string | null; revision: number }) => void
}) {
  const { t } = useTranslation()
  const [wave, setWave] = useState(item.wave)
  const [priority, setPriority] = useState(item.priority ?? '')
  const [status, setStatus] = useState<'idle' | 'saving' | 'error' | 'conflict'>('idle')

  const dirty = wave.trim() !== item.wave || priority.trim() !== (item.priority ?? '')

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!dirty || !wave.trim() || status === 'saving') return
    setStatus('saving')
    try {
      const result = await reassignBacklogTask(item.task_id, {
        wave: wave.trim(),
        priority: priority.trim() || null,
        expected_revision: item.revision,
      })
      setStatus('idle')
      onSaved({ wave: wave.trim(), priority: priority.trim() || null, revision: result.revision })
    } catch (error) {
      // `reassign`'s 409 body is `{reason, revision}` — not a string `detail`,
      // so `authedPost` falls back to the bare status code as the reason.
      setStatus(error instanceof Error && error.message.endsWith('-> 409') ? 'conflict' : 'error')
    }
  }

  return (
    <form onSubmit={submit} style={{ display: 'flex', gap: '.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
      <label style={{ display: 'flex', gap: '.35rem', alignItems: 'center', fontSize: '.72rem', color: 'var(--tx3)' }}>
        {t('backlogWave')}
        <input
          className="task-token-input"
          style={{ minWidth: '5rem', padding: '.3rem .5rem' }}
          value={wave}
          onChange={(event) => setWave(event.target.value)}
        />
      </label>
      <label style={{ display: 'flex', gap: '.35rem', alignItems: 'center', fontSize: '.72rem', color: 'var(--tx3)' }}>
        {t('backlogPriority')}
        <input
          className="task-token-input"
          style={{ minWidth: '4rem', padding: '.3rem .5rem' }}
          value={priority}
          onChange={(event) => setPriority(event.target.value)}
        />
      </label>
      <button type="submit" className="task-toggle" disabled={!dirty || status === 'saving'}>
        {status === 'saving' ? t('backlogReassignSaving') : t('backlogReassignSave')}
      </button>
      {status === 'conflict' && <span style={{ color: 'var(--bad)', fontSize: '.7rem' }}>{t('backlogReassignConflict')}</span>}
      {status === 'error' && <span style={{ color: 'var(--bad)', fontSize: '.7rem' }}>{t('backlogReassignError')}</span>}
    </form>
  )
}

function BacklogTaskRow({ item, onReassigned }: { item: BacklogTask; onReassigned: (id: string, next: { wave: string; priority: string | null; revision: number }) => void }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)

  return (
    <article className="task-row">
      <div style={{ display: 'flex', alignItems: 'center', gap: '.8rem', flexWrap: 'wrap' }}>
        <span className="task-status" style={{ color: backlogStatusTone(item.status) }}>
          {backlogStatusLabel(item.status, t)}
        </span>
        <strong style={{ color: 'var(--tx)', fontSize: '.84rem', flex: 1, minWidth: '8rem' }}>{item.title}</strong>
        <span className="task-date">
          {t('backlogWave')} {item.wave} · {t('backlogPriority')} {item.priority ?? t('backlogNoPriority')}
        </span>
        <button type="button" className="task-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? t('taskHide') : t('taskDetails')}
        </button>
      </div>
      {open && (
        <>
          <BacklogReassignForm item={item} onSaved={(next) => onReassigned(item.task_id, next)} />
          <div>
            <p className="task-details-label" style={{ margin: '0 0 .3rem' }}>{t('backlogHistory')}</p>
            <BacklogHistory taskId={item.task_id} />
          </div>
        </>
      )}
    </article>
  )
}

/** Chat-text intake (S6a): draft a proposed backlog line from free text,
 * let the owner read (and edit) it, then confirm. Voice (S6b) is a separate
 * transcription step feeding the same text box. */
function BacklogIntake({ onCreated }: { onCreated: () => void }) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [line, setLine] = useState<string | null>(null)
  const [parseFailed, setParseFailed] = useState(false)
  const [phase, setPhase] = useState<'idle' | 'drafting' | 'previewing' | 'confirming' | 'locked' | 'draftError' | 'confirmError' | 'duplicate' | 'success'>('idle')

  const draft = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!text.trim() || phase === 'drafting') return
    setPhase('drafting')
    try {
      const result: DraftResult = await draftBacklogTask(text.trim())
      setParseFailed(!result.ok)
      setLine(result.ok ? result.line : result.raw_output)
      setPhase('previewing')
    } catch (error) {
      setPhase(error instanceof QueueAuthError ? 'locked' : 'draftError')
    }
  }

  const confirm = async () => {
    if (!line || !line.trim() || phase === 'confirming') return
    setPhase('confirming')
    try {
      await confirmBacklogTask(line.trim())
      setPhase('success')
      setText('')
      setLine(null)
      onCreated()
    } catch (error) {
      if (error instanceof QueueAuthError) setPhase('locked')
      // `confirm`'s 409 body is a string `detail` ("... already exists ..."),
      // which `authedPost` surfaces verbatim as the reason.
      else if (error instanceof Error && error.message.includes('already exists')) setPhase('duplicate')
      else setPhase('confirmError')
    }
  }

  const cancel = () => {
    setLine(null)
    setPhase('idle')
  }

  return (
    <GlassPanel title={t('intakeTitle')}>
      {line === null ? (
        <form onSubmit={draft} style={{ display: 'flex', gap: '.6rem', flexWrap: 'wrap' }}>
          <textarea
            className="task-token-input"
            value={text}
            onChange={(event) => setText(event.target.value)}
            aria-label={t('intakeTitle')}
            placeholder={t('intakePlaceholder')}
            rows={2}
            style={{ width: '100%', resize: 'vertical', fontFamily: 'inherit' }}
          />
          <button type="submit" className="execution-back" disabled={!text.trim() || phase === 'drafting'}>
            {phase === 'drafting' ? t('intakeDrafting') : t('intakeDraft')}
          </button>
        </form>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '.6rem' }}>
          <p style={{ margin: 0, color: parseFailed ? 'var(--bad)' : 'var(--tx3)' }}>
            {parseFailed ? t('intakeParseFailed') : t('intakePreview')}
          </p>
          <textarea
            className="task-token-input"
            value={line}
            onChange={(event) => setLine(event.target.value)}
            aria-label={t('intakePreview')}
            rows={2}
            style={{ width: '100%', resize: 'vertical', fontFamily: 'var(--font-mono)' }}
          />
          <div style={{ display: 'flex', gap: '.6rem' }}>
            <button type="button" className="execution-back" onClick={confirm} disabled={!line.trim() || phase === 'confirming'}>
              {phase === 'confirming' ? t('intakeConfirming') : t('intakeConfirm')}
            </button>
            <button type="button" className="task-toggle" onClick={cancel}>{t('intakeCancel')}</button>
          </div>
        </div>
      )}
      {phase === 'locked' && <p style={{ color: 'var(--bad)', margin: 0 }}>{t('intakeLocked')}</p>}
      {phase === 'draftError' && <p style={{ color: 'var(--bad)', margin: 0 }}>{t('intakeDraftError')}</p>}
      {phase === 'confirmError' && <p style={{ color: 'var(--bad)', margin: 0 }}>{t('intakeError')}</p>}
      {phase === 'duplicate' && <p style={{ color: 'var(--bad)', margin: 0 }}>{t('intakeDuplicate')}</p>}
      {phase === 'success' && <p style={{ color: 'var(--ok)', margin: 0 }}>{t('intakeSuccess')}</p>}
    </GlassPanel>
  )
}

/** The central backlog's status and decomposition (S6c): an overall
 * done/total progress bar, per-status counts, and the task list itself —
 * each row IS one piece of the decomposition, with its own history and
 * reprioritization control. */
function BacklogSection() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<BacklogStatusCounts | null>(null)
  const [tasks, setTasks] = useState<BacklogTask[] | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(() => {
    setError(false)
    Promise.all([fetchBacklogStatus(), fetchBacklogTasks()])
      .then(([statusResult, tasksResult]) => {
        setStatus(statusResult)
        setTasks(tasksResult.tasks)
      })
      .catch(() => setError(true))
  }, [])
  useEffect(load, [load])

  const reassign = (taskId: string, next: { wave: string; priority: string | null; revision: number }) => {
    setTasks((current) =>
      current
        ? current.map((task) => (task.task_id === taskId ? { ...task, ...next } : task))
        : current,
    )
  }

  if (error) {
    return (
      <GlassPanel title={t('backlogTitle')}>
        <p style={{ color: 'var(--bad)' }}>{t('backlogErrorLoading')}</p>
        <button className="execution-back" onClick={load}>{t('retry')}</button>
      </GlassPanel>
    )
  }
  if (!status || !tasks) {
    return <GlassPanel title={t('backlogTitle')}>{t('loading')}</GlassPanel>
  }

  const done = status.counts['DONE'] ?? 0
  const percent = status.total === 0 ? 0 : Math.round((done / status.total) * 100)

  return (
    <>
      <BacklogIntake onCreated={load} />
      <GlassPanel title={t('backlogTitle')}>
        <p style={{ color: 'var(--tx3)', marginTop: 0 }}>{t('backlogSubtitle')}</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: '.6rem', marginBottom: '.9rem' }}>
          <div style={{ flex: 1, height: '.5rem', borderRadius: '999px', background: 'rgba(255,255,255,.08)', overflow: 'hidden' }}>
            <div style={{ width: `${percent}%`, height: '100%', background: 'var(--ok)' }} />
          </div>
          <span style={{ fontSize: '.72rem', color: 'var(--tx3)', whiteSpace: 'nowrap' }}>
            {percent}% {t('backlogProgress')} ({done}/{status.total})
          </span>
        </div>
        <div className="execution-filters" aria-label={t('allStates')}>
          {BACKLOG_STATUSES.map((state) => (
            <span key={state} style={{ color: 'var(--tx3)', fontSize: '.68rem' }}>
              {backlogStatusLabel(state, t)}: {status.counts[state] ?? 0}
            </span>
          ))}
        </div>
        <div className="execution-list">
          {tasks.length === 0 ? (
            <p style={{ color: 'var(--tx3)' }}>{t('backlogEmpty')}</p>
          ) : (
            tasks.map((item) => <BacklogTaskRow key={item.task_id} item={item} onReassigned={reassign} />)
          )}
        </div>
      </GlassPanel>
    </>
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
          {!locked && items && <BacklogSection />}
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
