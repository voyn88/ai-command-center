import { useTranslation } from 'react-i18next'
import { humanStatus, REPO_STATE_KEYS } from '../lib/labels'

type StatusRowProps = {
  project: string
  state: string | null
  unknownLabel: string
}

// Matches the real `repository_state` values emitted by
// `command_center/webapi/serializers.py` (`ok` / `unconfigured` /
// `invalid_path` / `not_git_repo`). `unconfigured` is not itself a health
// problem (no repository configured yet), so it stays neutral rather than
// warning-colored.
const REPO_STATE_COLOR: Record<string, string> = {
  ok: 'var(--ok)',
  unconfigured: 'var(--tx3-aa)',
  invalid_path: 'var(--bad)',
  not_git_repo: 'var(--bad)',
}

export default function StatusRow({ project, state, unknownLabel }: StatusRowProps) {
  const { t } = useTranslation()
  const status = humanStatus(REPO_STATE_KEYS, state, t)
  const color = state ? REPO_STATE_COLOR[state] || 'var(--tx3-aa)' : 'var(--tx3-aa)'
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0.55rem 0',
        borderBottom: '1px solid var(--gline)',
      }}
    >
      <span style={{ color: 'var(--tx)', fontSize: '0.84rem' }}>{project}</span>
      <span title={state || undefined} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.75rem', color: 'var(--tx2)' }}>
        <span style={{ width: 7, height: 7, borderRadius: '50%', background: color, display: 'inline-block' }} />
        {state ? status.label : unknownLabel}
      </span>
    </div>
  )
}
