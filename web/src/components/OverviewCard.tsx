type OverviewCardProps = {
  label: string
  value: number
}

// Quick-overview tile bound to real counts (`overview.reports_count` /
// `artifacts_count` / `recent_activity_count`) — no fabricated kanban lane
// counts (Backlog/In Progress/Done etc. do not exist in the backend DTO).
export default function OverviewCard({ label, value }: OverviewCardProps) {
  return (
    <div className="glass" style={{ padding: '0.9rem 1rem', textAlign: 'center' }}>
      <div style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--tx)' }}>{value}</div>
      <div style={{ fontSize: '0.72rem', color: 'var(--tx3-aa)', textTransform: 'uppercase', letterSpacing: '0.04em', marginTop: '0.2rem' }}>
        {label}
      </div>
    </div>
  )
}
