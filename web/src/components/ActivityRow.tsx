import { useTranslation } from 'react-i18next'
import type { ActivityItem } from '../lib/api'
import { formatRelativeAge } from '../lib/labels'

type ActivityRowProps = {
  item: ActivityItem
  fallbackLabel: string
  projectLabel?: string
}

// `activity` entries come from two merged upstream shapes (raw activity-log
// events and derived v2-run lifecycle events - see the `ActivityItem`
// comment in lib/api.ts), so most fields are optional. Raw event ids
// (`event_type`) are never shown as text: without a message we say "Event".
export default function ActivityRow({ item, fallbackLabel, projectLabel }: ActivityRowProps) {
  const { t } = useTranslation()
  const label = item.message || fallbackLabel
  const project = projectLabel ?? item.project
  const age = item.ts ? formatRelativeAge(item.ts, t) : null
  const raw = item.event_type || item.type
  return (
    <div title={raw || undefined} style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem', padding: '0.6rem 0', borderBottom: '1px solid var(--gline)' }}>
      <div style={{ color: 'var(--tx)', fontSize: '0.84rem' }}>{label}</div>
      <div style={{ display: 'flex', gap: '0.5rem', color: 'var(--tx3-aa)', fontSize: '0.75rem' }}>
        {project && <span>{project}</span>}
        {item.ts && <span title={item.ts}>{age ?? item.ts}</span>}
      </div>
    </div>
  )
}
