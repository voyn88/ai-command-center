// `glass` is the panel density (--glass) for this preset: light backgrounds need
// a denser pane so text keeps WCAG AA contrast (see lib/__tests__/contrast.test.ts).
export type BackgroundPreset = { id: string; labelKey: string; value: string; glass: string }

const GLASS_DEFAULT = 'rgba(17,22,38,.55)'

// Preset backgrounds matching the approved glass mockup: a dusk-city base
// ("auto") plus day/sunset/night/rain/snow/custom variants. Each `value` is
// a full CSS `background` shorthand string (stacked gradients). Consumed by
// both BackgroundLayer (the fixed full-viewport layer behind every panel)
// and BackgroundSwitcher (the footer control that swaps them by setting the
// `--bg` custom property on the document root) — single source of truth so
// the two never drift apart.
export const BACKGROUND_PRESETS: BackgroundPreset[] = [
  {
    id: 'auto',
    labelKey: 'bgAuto',
    glass: GLASS_DEFAULT,
    value:
      'radial-gradient(120% 80% at 78% 8%, rgba(255,175,120,.34) 0%, rgba(255,140,90,.10) 26%, transparent 52%),radial-gradient(90% 70% at 12% 4%, rgba(120,150,255,.28) 0%, transparent 50%),linear-gradient(180deg,#243056 0%,#1a2140 34%,#121732 60%,#0c1024 100%)',
  },
  {
    id: 'day',
    labelKey: 'bgDay',
    glass: 'rgba(17,22,38,.76)',
    value:
      'radial-gradient(100% 80% at 60% 0%, rgba(255,220,150,.5), transparent 55%),linear-gradient(180deg,#5b7fc7 0%,#7aa0d8 30%,#3a4d78 70%,#1a2340 100%)',
  },
  {
    id: 'sunset',
    labelKey: 'bgSunset',
    glass: 'rgba(17,22,38,.72)',
    value:
      'radial-gradient(120% 80% at 75% 10%, rgba(255,140,90,.55), transparent 50%),linear-gradient(180deg,#3a3f78 0%,#8a5a8e 40%,#c76a4e 72%,#2a1e34 100%)',
  },
  {
    id: 'night',
    labelKey: 'bgNight',
    glass: GLASS_DEFAULT,
    value:
      'radial-gradient(90% 60% at 20% 5%, rgba(90,120,220,.35), transparent 55%),linear-gradient(180deg,#0e1330 0%,#101740 45%,#0a0d22 100%)',
  },
  {
    id: 'rain',
    labelKey: 'bgRain',
    glass: GLASS_DEFAULT,
    value:
      'radial-gradient(100% 70% at 50% 0%, rgba(140,160,200,.25), transparent 55%),linear-gradient(180deg,#2a3350 0%,#333c58 45%,#171d2e 100%)',
  },
  {
    id: 'snow',
    labelKey: 'bgSnow',
    glass: 'rgba(17,22,38,.78)',
    value:
      'radial-gradient(100% 70% at 50% 0%, rgba(220,230,250,.5), transparent 55%),linear-gradient(180deg,#7d90b8 0%,#9fb2d4 40%,#40506e 78%,#1c2436 100%)',
  },
  {
    id: 'custom',
    labelKey: 'bgCustom',
    glass: GLASS_DEFAULT,
    value: 'repeating-linear-gradient(45deg,#1a2140,#1a2140 22px,#20284a 22px,#20284a 44px)',
  },
]

export const DEFAULT_BACKGROUND = BACKGROUND_PRESETS[0]
