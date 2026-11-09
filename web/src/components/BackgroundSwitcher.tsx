import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { BACKGROUND_PRESETS } from '../lib/backgroundPresets'
import type { BackgroundPreset } from '../lib/backgroundPresets'

type BackgroundSwitcherProps = { label: string }

// Swaps BackgroundLayer's fixed gradient by writing the `--bg` custom
// property on <html> — BackgroundLayer (rendered once at the app root)
// reads it via `background: var(--bg, …)`.
export default function BackgroundSwitcher({ label }: BackgroundSwitcherProps) {
  const { t } = useTranslation()
  const [active, setActive] = useState(BACKGROUND_PRESETS[0].id)

  function apply(preset: BackgroundPreset) {
    document.documentElement.style.setProperty('--bg', preset.value)
    document.documentElement.style.setProperty('--glass', preset.glass)
    setActive(preset.id)
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
      <span style={{ fontSize: '0.7rem', color: 'var(--tx3-aa)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</span>
      <div style={{ display: 'flex', gap: '0.4rem' }}>
        {BACKGROUND_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            title={t(p.labelKey)}
            aria-label={t(p.labelKey)}
            aria-pressed={active === p.id}
            onClick={() => apply(p)}
            className={`bg-swatch${active === p.id ? ' active' : ''}`}
            style={{
              width: 22,
              height: 22,
              borderRadius: '50%',
              border: '1px solid var(--gline)',
              padding: 0,
              cursor: 'pointer',
              backgroundImage: p.value,
            }}
          />
        ))}
      </div>
    </div>
  )
}
