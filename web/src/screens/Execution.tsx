import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { fetchExecution } from '../lib/api'
import type { ExecutionDTO, ExecutionRun } from '../lib/api'
import BackgroundSwitcher from '../components/BackgroundSwitcher'
import GlassPanel from '../components/GlassPanel'
import KpiCard from '../components/KpiCard'
import LangToggle from '../components/LangToggle'
import NavItem from '../components/NavItem'
import { ExecutionIcon, HomeIcon, TasksIcon } from '../components/NavIcons'

function formatDate(value: string | null, language: string, fallback: string) {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function formatDuration(value: number | null, fallback: string) {
  if (value === null || value === undefined) return fallback
  if (value < 60) return `${Math.round(value)}s`
  const minutes = Math.floor(value / 60)
  const seconds = Math.round(value % 60)
  return `${minutes}m ${seconds}s`
}

function stateTone(state: string) {
  if (state === 'COMPLETED') return 'var(--ok)'
  if (['FAILED', 'INTERRUPTED', 'UNKNOWN'].includes(state)) return 'var(--bad)'
  if (['RUNNING', 'QUEUED', 'PREPARED'].includes(state)) return 'var(--accent-2)'
  return 'var(--tx3)'
}

function RunRow({ run, fallback, language, resultLabel, exitLabel }: { run: ExecutionRun; fallback: string; language: string; resultLabel: string; exitLabel: string }) {
  const result = run.verdict || run.failure_reason || (run.exit_code !== null ? `${exitLabel}: ${run.exit_code}` : fallback)
  return (
    <article className="execution-row">
      <div className="execution-primary">
        <span className="execution-state" style={{ color: stateTone(run.state) }}>{run.state}</span>
        <strong>{run.title}</strong>
        <span>{run.project_name}</span>
      </div>
      <div className="execution-meta">
        <span>{formatDate(run.started_at || run.created_at, language, fallback)}</span>
        <span>{formatDuration(run.duration_seconds, fallback)}</span>
        <span title={resultLabel}>{result}</span>
      </div>
    </article>
  )
}

export default function Execution({ onNavigate }: { onNavigate: (screen: 'home' | 'execution' | 'tasks') => void }) {
  const { t, i18n } = useTranslation()
  const [data, setData] = useState<ExecutionDTO | null>(null)
  const [error, setError] = useState(false)
  const [state, setState] = useState('ALL')

  const load = () => {
    setError(false)
    fetchExecution().then(setData).catch(() => setError(true))
  }
  useEffect(load, [])

  const states = useMemo(() => data ? Object.entries(data.state_counts).filter(([, count]) => count > 0) : [], [data])
  const runs = data?.runs.filter((run) => state === 'ALL' || run.state === state) || []

  return (
    <div style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <header className="glass execution-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem' }}>
          <div className="mark" aria-hidden="true" />
          <div><h1>{t('executionTitle')}</h1><p>{t('executionSubtitle')}</p></div>
        </div>
        <button type="button" className="execution-back" onClick={() => onNavigate('home')}>{t('backHome')}</button>
      </header>

      <div className="execution-layout">
        <aside className="glass execution-nav">
          <NavItem label={t('home')} icon={<HomeIcon />} onClick={() => onNavigate('home')} />
          <NavItem label={t('execution')} icon={<ExecutionIcon />} active onClick={() => onNavigate('execution')} />
          <NavItem label={t('tasks')} icon={<TasksIcon />} onClick={() => onNavigate('tasks')} />
        </aside>
        <main className="execution-main">
          {!data && !error && <GlassPanel>{t('loading')}</GlassPanel>}
          {error && <GlassPanel><p style={{ color: 'var(--bad)' }}>{t('errorLoading')}</p><button className="execution-back" onClick={load}>{t('retry')}</button></GlassPanel>}
          {data && <>
            <div className="kpi-grid">
              <KpiCard label={t('visibleRuns')} value={data.summary.visible_runs} meta={t('runHistory')} />
              <KpiCard label={t('activeRuns')} value={data.summary.active} meta={t('running')} />
              <KpiCard label={t('completedRuns')} value={data.summary.completed} meta={t('completedRuns')} />
              <KpiCard label={t('needsAttentionRuns')} value={data.summary.needs_attention} meta={t('needsAttention')} />
            </div>
            <GlassPanel title={t('runHistory')}>
              <div className="execution-filters" aria-label={t('allStates')}>
                <button className={state === 'ALL' ? 'active' : ''} onClick={() => setState('ALL')}>{t('allStates')} · {data.summary.visible_runs}</button>
                {states.map(([name, count]) => <button key={name} className={state === name ? 'active' : ''} onClick={() => setState(name)}>{name} · {count}</button>)}
              </div>
              <div className="execution-list">
                {runs.length === 0 ? <p style={{ color: 'var(--tx3)' }}>{t('noRuns')}</p> : runs.map((run) => <RunRow key={`${run.source}-${run.id}`} run={run} fallback={t('notAvailable')} language={i18n.language} resultLabel={t('result')} exitLabel={t('exitCode')} />)}
              </div>
            </GlassPanel>
          </>}
        </main>
      </div>
      <footer className="glass execution-footer"><BackgroundSwitcher label={t('background')} /><LangToggle /></footer>
    </div>
  )
}
