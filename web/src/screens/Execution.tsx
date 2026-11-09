import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { fetchExecution } from '../lib/api'
import type { ExecutionDTO, ExecutionRun } from '../lib/api'
import BackgroundSwitcher from '../components/BackgroundSwitcher'
import GlassPanel from '../components/GlassPanel'
import TechDetails from '../components/TechDetails'
import KpiCard from '../components/KpiCard'
import LangToggle from '../components/LangToggle'
import NavItem from '../components/NavItem'
import { ExecutionIcon, HomeIcon, TasksIcon } from '../components/NavIcons'
import { ATTENTION_STATES, formatDurationRu, formatOutcomeReason, humanStatus, RUN_STATE_KEYS, runStateColor, VERDICT_KEYS } from '../lib/labels'

function formatDate(value: string | null, language: string, fallback: string) {
  if (!value) return fallback
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function RunRow({ run, fallback, language }: { run: ExecutionRun; fallback: string; language: string }) {
  const { t } = useTranslation()
  const status = humanStatus(RUN_STATE_KEYS, run.state, t)
  const verdict = run.verdict ? humanStatus(VERDICT_KEYS, run.verdict, t) : null
  const result = verdict?.label || (run.failure_reason ? formatOutcomeReason(run.failure_reason, t) : fallback)
  // Raw exit code and failure reason stay reachable, but only as a tooltip.
  const resultTitle = [
    run.exit_code !== null ? `${t('exitCode')}: ${run.exit_code}` : '',
    run.failure_reason || '',
    verdict?.raw || '',
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
      <TechDetails
        title={t('techTitle')}
        hint={t('techHint')}
        items={[
          [t('techState'), run.state],
          [t('techFailure'), run.failure_reason],
          [t('techExit'), run.exit_code],
          [t('techVerdict'), run.verdict],
          [t('techRunId'), run.id],
          [t('techSource'), run.source],
        ]}
      />
    </article>
  )
}

const ATTENTION = 'ATTENTION' // virtual chip: FAILED + INTERRUPTED + UNKNOWN

function runRank(state: string): number {
  if (ATTENTION_STATES.includes(state)) return 0
  if (['RUNNING', 'QUEUED', 'PREPARED'].includes(state)) return 1
  return 2
}

export default function Execution({ onNavigate }: { onNavigate: (screen: 'home' | 'execution' | 'tasks') => void }) {
  const { t, i18n } = useTranslation()
  const [data, setData] = useState<ExecutionDTO | null>(null)
  const [error, setError] = useState(false)
  const [selected, setSelected] = useState('ALL')
  const mainRef = useRef<HTMLElement>(null)

  const load = () => {
    setError(false)
    fetchExecution().then(setData).catch(() => setError(true))
  }
  useEffect(load, [])
  const retry = () => { mainRef.current?.focus(); load() }

  // Everything on screen is counted from the rows we actually have (data.runs),
  // never from the server's summary, so tiles, banner, chips and list agree.
  const allRuns = useMemo(() => data?.runs || [], [data])
  const states = useMemo(() => {
    const counts = new Map<string, number>()
    for (const run of allRuns) counts.set(run.state, (counts.get(run.state) ?? 0) + 1)
    return [...counts.entries()]
  }, [allRuns])
  const attentionCount = allRuns.filter((run) => ATTENTION_STATES.includes(run.state)).length
  const activeCount = allRuns.filter((run) => ['PREPARED', 'QUEUED', 'RUNNING'].includes(run.state)).length
  const completedCount = allRuns.filter((run) => run.state === 'COMPLETED').length
  // A chip whose rows vanished after a refresh must not leave the list stuck empty.
  const state = selected === 'ALL' || (selected === ATTENTION && attentionCount > 0) || states.some(([name]) => name === selected) ? selected : 'ALL'
  const setState = setSelected
  const runs = useMemo(() => {
    const list = allRuns.filter((run) => state === 'ALL' || (state === ATTENTION ? ATTENTION_STATES.includes(run.state) : run.state === state))
    // Stable sort: attention first, then running, then the rest (only for "all").
    return state === 'ALL' ? [...list].sort((a, b) => runRank(a.state) - runRank(b.state)) : list
  }, [allRuns, state])

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
        <main className="execution-main" tabIndex={-1} ref={mainRef}>
          {!data && !error && <GlassPanel><span role="status">{t('loading')}</span></GlassPanel>}
          {error && <GlassPanel><p role="alert" style={{ color: 'var(--bad-text)' }}>{t('errorLoading')} {t('errorLoadingHint')}</p><button className="execution-back" onClick={retry}>{t('retry')}</button></GlassPanel>}
          {data && <>
            <div className="kpi-grid">
              <KpiCard label={t('visibleRuns')} value={allRuns.length} meta={t('runHistory')} />
              <KpiCard label={t('activeRuns')} value={activeCount} meta={t('running')} />
              <KpiCard label={t('completedRuns')} value={completedCount} meta={t('completedRuns')} />
              <KpiCard label={t('needsAttentionRuns')} value={attentionCount} meta={t('needsAttention')} />
            </div>
            {attentionCount > 0 && <div className="glass" role="status" style={{ padding: '.8rem 1.1rem', color: 'var(--warn)', fontWeight: 600, display: 'flex', gap: '.8rem', alignItems: 'center', flexWrap: 'wrap' }}><span>{t('attentionBanner', { n: attentionCount })}</span><button type="button" className="execution-back" onClick={() => setState(ATTENTION)}>{t('attentionShow')}</button></div>}
            <GlassPanel title={t('runHistory')}>
              <div className="execution-filters" role="group" aria-label={t('allStates')}>
                <button aria-pressed={state === 'ALL'} className={state === 'ALL' ? 'active' : ''} onClick={() => setState('ALL')}>{t('allStates')} · {allRuns.length}</button>
                {attentionCount > 0 && <button aria-pressed={state === ATTENTION} className={state === ATTENTION ? 'active' : ''} onClick={() => setState(ATTENTION)}>{t('needsAttentionRuns')} · {attentionCount}</button>}
                {states.map(([name, count]) => <button key={name} aria-pressed={state === name} title={name} className={state === name ? 'active' : ''} onClick={() => setState(name)}>{humanStatus(RUN_STATE_KEYS, name, t).label} · {count}</button>)}
              </div>
              <div className="execution-list">
                {runs.length === 0 ? <div><p style={{ color: 'var(--tx3-aa)' }}>{t('noRuns')}</p>{state !== 'ALL' && <button type="button" className="execution-back" onClick={() => setState('ALL')}>{t('resetFilters')}</button>}</div> : runs.map((run) => <RunRow key={`${run.source}-${run.id}`} run={run} fallback={t('notAvailable')} language={i18n.language} />)}
              </div>
            </GlassPanel>
          </>}
        </main>
      </div>
      <footer className="glass execution-footer"><BackgroundSwitcher label={t('background')} /><LangToggle /></footer>
    </div>
  )
}
