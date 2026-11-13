type AssistantPanelProps = {
  title: string
  placeholder: string
  suggestionsLabel: string
  suggestions: string[]
  toolsLabel: string
  tools: string[]
}

// Static decorative shell only — there is no AI backend wired in this
// slice. No live values (no weather/time/answers) are rendered here; the
// suggestion chips and capability list are fixed, non-interactive
// placeholders, and the panel is honestly labelled as a preview rather than
// claiming to be "online".
export default function AssistantPanel({ title, placeholder, suggestionsLabel, suggestions, toolsLabel, tools }: AssistantPanelProps) {
  return (
    <div className="glass" style={{ padding: '1.25rem', display: 'flex', flexDirection: 'column', gap: '1.1rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <div className="orb" aria-hidden="true" />
        <div>
          <div style={{ color: 'var(--tx)', fontWeight: 600, fontSize: '0.92rem' }}>{title}</div>
          <div style={{ color: 'var(--tx3-aa)', fontSize: '0.72rem', marginTop: '0.15rem' }}>{placeholder}</div>
        </div>
      </div>

      <div>
        <div
          style={{
            fontSize: '0.7rem',
            fontWeight: 600,
            color: 'var(--tx3-aa)',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
            marginBottom: '0.5rem',
          }}
        >
          {suggestionsLabel}
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem' }}>
          {suggestions.map((s) => (
            <span
              key={s}
              style={{
                fontSize: '0.72rem',
                color: 'var(--tx2)',
                border: '1px solid var(--gline)',
                borderRadius: 999,
                padding: '0.3rem 0.65rem',
              }}
            >
              {s}
            </span>
          ))}
        </div>
      </div>

      <div>
        <div
          style={{
            fontSize: '0.7rem',
            fontWeight: 600,
            color: 'var(--tx3-aa)',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
            marginBottom: '0.5rem',
          }}
        >
          {toolsLabel}
        </div>
        <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
          {tools.map((tool) => (
            <li key={tool} style={{ fontSize: '0.78rem', color: 'var(--tx2)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--tx3-aa)', display: 'inline-block' }} />
              {tool}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
