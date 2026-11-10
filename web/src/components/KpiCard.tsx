type KpiCardProps = {
  label: string
  value: number | string
  meta?: string
  /** Opt-in decorative trend line; off by default (no real trend data exists). */
  trend?: boolean
}

// Static, flat sparkline: no trend/history data exists anywhere in the
// backend snapshot (see `Kpi` in lib/api.ts), so this intentionally renders
// a flat line rather than fabricate a trend — per the data contract's
// "never fabricate data" rule.
function FlatSparkline() {
  return (
    <svg
      viewBox="0 0 100 20"
      preserveAspectRatio="none"
      aria-hidden="true"
      style={{ width: '100%', height: 18, marginTop: '0.6rem', display: 'block' }}
    >
      <line x1="0" y1="10" x2="100" y2="10" stroke="var(--accent-2)" strokeWidth="2" strokeLinecap="round" opacity="0.35" />
    </svg>
  )
}

export default function KpiCard({ label, value, meta, trend = false }: KpiCardProps) {
  return (
    <div className="glass" style={{ padding: '1.1rem' }}>
      <div style={{ fontSize: '0.7rem', fontWeight: 600, letterSpacing: '0.04em', color: 'var(--tx3-aa)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div style={{ fontSize: '1.9rem', fontWeight: 700, color: 'var(--tx)', marginTop: '0.35rem' }}>{value}</div>
      {meta && <div style={{ fontSize: '0.78rem', color: 'var(--tx2)', marginTop: '0.2rem' }}>{meta}</div>}
      {trend && <FlatSparkline />}
    </div>
  )
}
