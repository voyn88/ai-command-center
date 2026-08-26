import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { fetchHome } from '../lib/api'
import type { HomeDTO, Kpi } from '../lib/api'
import GlassPanel from '../components/GlassPanel'
import KpiCard from '../components/KpiCard'
import QueueRow from '../components/QueueRow'
import HealthDonut from '../components/HealthDonut'
import ActivityRow from '../components/ActivityRow'
import OverviewCard from '../components/OverviewCard'
import QuickActionButton from '../components/QuickActionButton'
import AssistantPanel from '../components/AssistantPanel'
import StatusRow from '../components/StatusRow'
import NavItem from '../components/NavItem'
import BackgroundSwitcher from '../components/BackgroundSwitcher'
import LangToggle from '../components/LangToggle'
import {
  HomeIcon,
  WorkspaceIcon,
  AgentsIcon,
  ExecutionIcon,
  GitIcon,
  TasksIcon,
  ReportsIcon,
  ArtifactsIcon,
  ReviewCenterIcon,
  SettingsIcon,
} from '../components/NavIcons'

const NAV_KEYS = [
  'home',
  'workspace',
  'agents',
  'execution',
  'git',
  'tasks',
  'reports',
  'artifacts',
  'reviewCenter',
  'settings',
] as const

// Parallel to NAV_KEYS — one icon component per nav item, in the same order.
const NAV_ICONS = [
  HomeIcon,
  WorkspaceIcon,
  AgentsIcon,
  ExecutionIcon,
  GitIcon,
  TasksIcon,
  ReportsIcon,
  ArtifactsIcon,
  ReviewCenterIcon,
  SettingsIcon,
] as const

// Pairs `meta_n` with `meta_key` for the agents/tasks/reviews KPIs
// ("2 running", "5 in progress", "1 pending"). NOT used for the Projects
// KPI — see `formatProjectsMeta` below.
function formatKpiMeta(kpi: Kpi, t: (key: string) => string): string {
  if (kpi.meta_n !== undefined) return `${kpi.meta_n} ${t(kpi.meta_key)}`
  return t(kpi.meta_key)
}

// The backend's `kpis.projects.meta_key` is ALWAYS the literal string
// "all_healthy" (see serializers.py `_kpis`), regardless of whether every
// project is actually healthy — it's a fixed field name, not a computed
// verdict. Trusting it directly would let the KPI card claim "All healthy"
// while some projects are unhealthy. The real rollup lives in
// `health.{projects_healthy,projects_total}` (the same source
// `HealthDonut` uses), so we derive the Projects KPI's meta text from that
// instead of from `meta_key`.
function formatProjectsMeta(healthy: number, total: number, t: (key: string) => string): string {
  if (total > 0 && healthy === total) return t('all_healthy')
  return `${healthy} ${t('healthyCount')}`
}

function EmptyState({ label }: { label: string }) {
  return <div style={{ color: 'var(--tx3)', fontSize: '0.8rem', padding: '0.75rem 0' }}>{label}</div>
}

function CenteredPanel({ children }: { children: ReactNode }) {
  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1.5rem' }}>
      <GlassPanel>{children}</GlassPanel>
    </div>
  )
}

export default function Home({ onNavigate = () => undefined }: { onNavigate?: (screen: 'home' | 'execution' | 'tasks') => void }) {
  const { t } = useTranslation()
  const [data, setData] = useState<HomeDTO | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  function load() {
    setLoading(true)
    setError(null)
    fetchHome()
      .then((dto) => setData(dto))
      .catch(() => setError(t('errorLoading')))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    // Fetch exactly once on mount; `load` is stable enough for this purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (loading) {
    return <CenteredPanel>{t('loading')}</CenteredPanel>
  }

  if (error || !data) {
    return (
      <CenteredPanel>
        <div style={{ color: 'var(--bad)', marginBottom: '0.75rem' }}>{error || t('errorLoading')}</div>
        <button
          type="button"
          onClick={load}
          className="glass"
          style={{ padding: '0.4rem 0.9rem', color: 'var(--tx)', cursor: 'pointer', background: 'rgba(255,255,255,0.02)' }}
        >
          {t('retry')}
        </button>
      </CenteredPanel>
    )
  }

  const projectNameById = new Map(data.projects.map((p) => [p.id, p.name]))
  const projectLabel = (id: string) => projectNameById.get(id) || id

  return (
    <div style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      <header className="glass" style={{ padding: '1rem 1.5rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.75rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem' }}>
          <div className="mark" aria-hidden="true" />
          <div>
            <div style={{ color: 'var(--tx)', fontWeight: 700, fontSize: '1rem' }}>AI Command Center</div>
            <div style={{ color: 'var(--tx2)', fontSize: '0.8rem', marginTop: '0.15rem' }}>{t('greeting')}</div>
            <div style={{ color: 'var(--tx3)', fontSize: '0.72rem' }}>{t('greetingSub')}</div>
          </div>
        </div>
        <button
          type="button"
          disabled
          title={t('comingSoon')}
          style={{
            background: 'linear-gradient(135deg, var(--accent-1), var(--accent-2))',
            color: '#fff',
            border: 'none',
            borderRadius: 10,
            padding: '0.55rem 1.1rem',
            fontSize: '0.82rem',
            fontWeight: 600,
            cursor: 'not-allowed',
            opacity: 0.85,
          }}
        >
          {t('newTask')}
        </button>
      </header>

      <div className="layout-grid">
        <aside
          className="glass"
          style={{ padding: '1rem', display: 'flex', flexDirection: 'column', gap: '0.25rem', position: 'sticky', top: '1.5rem' }}
        >
          {NAV_KEYS.map((key, i) => {
            const Icon = NAV_ICONS[i]
            return <NavItem key={key} label={t(key)} icon={<Icon />} active={i === 0} onClick={key === 'home' || key === 'execution' || key === 'tasks' ? () => onNavigate(key) : undefined} />
          })}
        </aside>

        <main style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', minWidth: 0 }}>
          <div className="kpi-grid">
            <KpiCard
              label={t('projects')}
              value={data.kpis.projects.value}
              meta={formatProjectsMeta(data.health.projects_healthy, data.health.projects_total, t)}
            />
            <KpiCard label={t('agents')} value={data.kpis.agents.value} meta={formatKpiMeta(data.kpis.agents, t)} />
            <KpiCard label={t('tasks')} value={data.kpis.tasks.value} meta={formatKpiMeta(data.kpis.tasks, t)} />
            <KpiCard label={t('reviews')} value={data.kpis.reviews.value} meta={formatKpiMeta(data.kpis.reviews, t)} />
          </div>

          <div className="split-grid">
            <GlassPanel title={t('executionQueue')}>
              {data.queue.length === 0 ? (
                <EmptyState label={t('queueEmpty')} />
              ) : (
                data.queue.map((item, i) => (
                  <QueueRow
                    key={`${item.project}-${item.title}-${i}`}
                    item={item}
                    projectLabel={projectLabel(item.project)}
                    unknownStateLabel={t('unknownState')}
                  />
                ))
              )}
            </GlassPanel>

            <GlassPanel title={t('projectHealth')}>
              <div style={{ display: 'flex', justifyContent: 'center', marginBottom: '0.9rem' }}>
                <HealthDonut healthy={data.health.projects_healthy} total={data.health.projects_total} />
              </div>
              {data.projects.length === 0 ? (
                <EmptyState label={t('noProjects')} />
              ) : (
                data.projects.map((p) => (
                  <div
                    key={p.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '0.4rem 0',
                      borderBottom: '1px solid var(--gline)',
                    }}
                  >
                    <span style={{ color: 'var(--tx)', fontSize: '0.82rem' }}>{p.name}</span>
                    {p.redacted ? (
                      <span style={{ fontSize: '0.68rem', color: 'var(--tx3)' }}>{t('restricted')}</span>
                    ) : (
                      <span style={{ fontSize: '0.68rem', color: p.healthy ? 'var(--ok)' : 'var(--bad)' }}>
                        {p.healthy ? t('healthy') : t('needsAttention')}
                      </span>
                    )}
                  </div>
                ))
              )}
            </GlassPanel>
          </div>

          <div className="split-grid">
            <GlassPanel title={t('recentActivity')}>
              {data.activity.length === 0 ? (
                <EmptyState label={t('activityEmpty')} />
              ) : (
                data.activity.map((item, i) => (
                  <ActivityRow key={item.id || `${item.ts || ''}-${i}`} item={item} fallbackLabel={t('activityEmpty')} />
                ))
              )}
            </GlassPanel>

            <GlassPanel title={t('systemStatus')}>
              {data.status.length === 0 ? (
                <EmptyState label={t('noProjects')} />
              ) : (
                data.status.map((s, i) => (
                  <StatusRow
                    key={`${s.project}-${i}`}
                    project={projectLabel(s.project)}
                    state={s.repository_state}
                    unknownLabel={t('unknownState')}
                  />
                ))
              )}
            </GlassPanel>
          </div>

          <GlassPanel title={t('quickOverview')}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '0.9rem' }}>
              <OverviewCard label={t('reports')} value={data.overview.reports_count} />
              <OverviewCard label={t('artifacts')} value={data.overview.artifacts_count} />
              <OverviewCard label={t('recentActivity')} value={data.overview.recent_activity_count} />
            </div>
          </GlassPanel>

          <GlassPanel title={t('quickActions')}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '0.75rem' }}>
              <QuickActionButton label={t('newTask')} badge={t('comingSoon')} />
              <QuickActionButton label={t('reports')} badge={t('comingSoon')} />
              <QuickActionButton label={t('artifacts')} badge={t('comingSoon')} />
              <QuickActionButton label={t('reviewCenter')} badge={t('comingSoon')} />
            </div>
          </GlassPanel>
        </main>

        <aside style={{ position: 'sticky', top: '1.5rem' }}>
          <AssistantPanel
            title={t('assistant')}
            placeholder={t('assistantPlaceholder')}
            suggestionsLabel={t('suggestions')}
            suggestions={[t('suggestionReview'), t('suggestionSummarize'), t('suggestionHealth')]}
            toolsLabel={t('tools')}
            tools={[t('toolCodeReview'), t('toolTaskTriage'), t('toolGitOps')]}
          />
        </aside>
      </div>

      <footer className="glass" style={{ padding: '0.75rem 1.25rem', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.75rem' }}>
        <BackgroundSwitcher label={t('background')} />
        <LangToggle />
      </footer>
    </div>
  )
}
