type HealthDonutProps = {
  healthy: number
  total: number
  size?: number
  label?: string
}

// Pure SVG stroke-dasharray donut rendering the REAL workspace rollup
// (`health.projects_healthy` / `health.projects_total`) — never the
// fabricated 94%-style per-dimension score from the original mockup.
export default function HealthDonut({ healthy, total, size = 120, label }: HealthDonutProps) {
  const percent = total > 0 ? Math.round((healthy / total) * 100) : 0
  const stroke = 10
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const dash = (percent / 100) * circumference

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label ?? `${percent}%`}>
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--gline)" strokeWidth={stroke} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="var(--ok)"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={`${dash} ${circumference - dash}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central" fontSize={size * 0.2} fontWeight={700} fill="var(--tx)">
        {percent}%
      </text>
    </svg>
  )
}
