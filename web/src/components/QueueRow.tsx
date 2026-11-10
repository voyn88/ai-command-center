import { useTranslation } from 'react-i18next'
import type { QueueItem } from '../lib/api'
import { humanStatus, RUN_STATE_KEYS, runStateColor } from '../lib/labels'

type QueueRowProps = {
  item: QueueItem
  projectLabel: string
  unknownStateLabel: string
}

export default function QueueRow({ item, projectLabel, unknownStateLabel }: QueueRowProps) {
  const { t } = useTranslation()
  const state = (item.state || '').toUpperCase()
  const color = runStateColor(state)
  const status = humanStatus(RUN_STATE_KEYS, state, t)
  // `progress` always reports 0 today — no upstream progress signal exists
  // yet (see the `QueueItem` comment in lib/api.ts). Only render a bar/percent
  // when a value is actually nonzero, so today's real data never shows a
  // permanently-empty, fabricated-looking progress indicator.
  const showProgress = item.progress > 0

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '0.75rem',
        padding: '0.65rem 0',
        borderBottom: '1px solid var(--gline)',
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            color: 'var(--tx)',
            fontSize: '0.86rem',
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          <span title={item.title}>{item.title}</span>
        </div>
        <div style={{ color: 'var(--tx3-aa)', fontSize: '0.75rem', marginTop: '0.15rem' }}>{projectLabel}</div>
      </div>
      {showProgress && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', width: 96 }}>
          <div style={{ flex: 1, height: 4, borderRadius: 2, background: 'var(--gline)', overflow: 'hidden' }}>
            <div style={{ width: `${Math.min(100, item.progress)}%`, height: '100%', background: color, borderRadius: 2 }} />
          </div>
          <span style={{ fontSize: '0.72rem', color: 'var(--tx2)', fontVariantNumeric: 'tabular-nums' }}>{item.progress}%</span>
        </div>
      )}
      <span
        title={status.raw || undefined}
        style={{
          fontSize: '0.75rem',
          fontWeight: 600,
          color,
          border: `1px solid ${color}`,
          borderRadius: 999,
          padding: '0.15rem 0.55rem',
          whiteSpace: 'nowrap',
        }}
      >
        {state ? status.label : unknownStateLabel}
      </span>
    </div>
  )
}
