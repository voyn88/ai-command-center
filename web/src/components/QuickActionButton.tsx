type QuickActionButtonProps = {
  label: string
  badge?: string
}

// This slice's API is read-only (no push/PR/merge/launch/create endpoints
// exist yet), so quick actions render as an inert preview rather than claim
// functionality the backend does not provide.
export default function QuickActionButton({ label, badge }: QuickActionButtonProps) {
  return (
    <button
      type="button"
      disabled
      aria-disabled="true"
      title={badge}
      className="glass"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
        gap: '0.3rem',
        padding: '0.75rem 0.9rem',
        color: 'var(--tx2)',
        cursor: 'not-allowed',
        background: 'rgba(255,255,255,0.02)',
        font: 'inherit',
        textAlign: 'left',
      }}
    >
      <span style={{ fontSize: '0.82rem', fontWeight: 600, color: 'var(--tx)' }}>{label}</span>
      {badge && <span style={{ fontSize: '0.66rem', color: 'var(--tx3-aa)' }}>{badge}</span>}
    </button>
  )
}
