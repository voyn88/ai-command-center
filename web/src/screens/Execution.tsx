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
import { ATTENTION_STATES, formatDurationRu, formatOutcomeReason, humanStatus, RUN_STATE_KEYS, runStateColor } from '../lib/labels'

function formatDate(value: string | null, language: string, fallback: string) {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function RunRow({ run, fallback, language }: { run: ExecutionRun; fallback: string; language: string }) {
  const { t } = useTranslation()
  const status = humanStatus(RUN_STATE_KEYS, run.state, t)
  const result = run.verdict || (run.failure_reason ? formatOutcomeReason(run.failure_reason, t) : fallback)
  // Raw exit code and failure reason stay reachable, but only as a tooltip.
  const resultTitle = [
    run.exit_code !== null ? `${t('exitCode')}: ${run.exit_code}` : '',
    run.failure_reason || '',
  ].filter(Boolean).join(' · ') || t('result')
  return (
    <article className="execution-row">
      <div className="execution-primary">
        <span className="execution-state" title={status.raw} style={{ color: runStateColor(run.state) }}>{status.label}</span>
        <strong title={run.title}>{run.title}</strong>
        <span>{run.project_name}</span>
      </div>
      <div className="execution-meta">
        <span>{formatDate(run.started_at || run.created_at, language, fallback)}</span>
        <span>{formatDurationRu(run.duration_seconds, t, fallback)}</span>
        <span title={resultTitle}>{result}</span>
      </div>
    </article>
  )
}

function runRank(state: string): number {
  if (ATTENTION_STATES.includes(state)) return 0
  if (['RUNNING', 'QUEUED', 'PREPARED'].includes(state)) return 1
  return 2
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
  const runs = useMemo(() => {
    const list = data?.runs.filter((run) => state === 'ALL' || run.state === state) || []
    // Stable sort: attention first, then running, then the rest (only for "all").
    return state === 'ALL' ? [...list].sort((a, b) => runRank(a.state) - runRank(b.state)) : list
  }, [data, state])
  const attentionCount = data?.summary.needs_attention ?? 0

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
          {!data && !error && <GlassPanel><span role="status">{t('loading')}</span></GlassPanel>}
          {error && <GlassPanel><p role="alert" style={{ color: 'var(--bad)' }}>{t('errorLoading')} {t('errorLoadingHint')}</p><button className="execution-back" onClick={load}>{t('retry')}</button></GlassPanel>}
          {data && <>
            <div className="kpi-grid">
              <KpiCard label={t('visibleRuns')} value={data.summary.visible_runs} meta={t('runHistory')} />
              <KpiCard label={t('activeRuns')} value={data.summary.active} meta={t('running')} />
              <KpiCard label={t('completedRuns')} value={data.summary.completed} meta={t('completedRuns')} />
              <KpiCard label={t('needsAttentionRuns')} value={data.summary.needs_attention} meta={t('needsAttention')} />
            </div>
            {attentionCount > 0 && <div className="glass" role="status" style={{ padding: '.8rem 1.1rem', color: 'var(--warn)', fontWeight: 600 }}>{t('attentionBanner', { n: attentionCount })}</div>}
            <GlassPanel title={t('runHistory')}>
              <div className="execution-filters" role="group" aria-label={t('allStates')}>
                <button aria-pressed={state === 'ALL'} className={state === 'ALL' ? 'active' : ''} onClick={() => setState('ALL')}>{t('allStates')} · {data.summary.visible_runs}</button>
                {states.map(([name, count]) => <button key={name} aria-pressed={state === name} title={name} className={state === name ? 'active' : ''} onClick={() => setState(name)}>{humanStatus(RUN_STATE_KEYS, name, t).label} · {count}</button>)}
              </div>
              <div className="execution-list">
                {runs.length === 0 ? <p style={{ color: 'var(--tx3-aa)' }}>{t('noRuns')}</p> : runs.map((run) => <RunRow key={`${run.source}-${run.id}`} run={run} fallback={t('notAvailable')} language={i18n.language} />)}
              </div>
            </GlassPanel>
          </>}
        </main>
      </div>
      <footer className="glass execution-footer"><BackgroundSwitcher label={t('background')} /><LangToggle /></footer>
    </div>
  )
}
