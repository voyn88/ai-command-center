import type { ReactNode } from 'react'

type NavItemProps = {
  label: string
  icon: ReactNode
  active?: boolean
  onClick?: () => void
  title?: string
}

export default function NavItem({ label, icon, active, onClick, title }: NavItemProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      className="nav-item"
      aria-current={active ? 'page' : undefined}
      title={!onClick ? title : undefined}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '0.65rem',
        padding: '0.55rem 0.7rem',
        borderRadius: 10,
        color: active ? 'var(--tx)' : 'var(--tx3-aa)',
        background: active ? 'color-mix(in srgb, var(--accent-1) 16%, transparent)' : 'transparent',
        fontSize: '0.82rem',
        fontWeight: active ? 600 : 500,
        border: 0,
        width: '100%',
        cursor: onClick ? 'pointer' : 'default',
        textAlign: 'left',
      }}
    >
      <span style={{ display: 'inline-flex', width: 16, color: active ? 'var(--accent-2)' : 'inherit' }}>{icon}</span>
      {label}
    </button>
  )
}
