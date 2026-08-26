# Design tokens & primitives

One source of truth for the operator dashboard's look — consumed by **both** the
desktop web shell and the mobile app. Dark-first, electric-indigo accent,
ink-slate ground, depth over borders, small SF/mono type on a 12-column grid.

## Files

| File | Role |
| --- | --- |
| `tokens.json` | Canonical tokens. Edit **only** this to change the design. |
| `tokens.css` | Generated CSS custom properties. Do not edit by hand. |
| `primitives.css` | Component primitives styled purely from tokens. |
| `build_tokens.py` | Regenerates `tokens.css` from `tokens.json`. |

## The one rule

**No hardcoded colors. Tokens only.** Every downstream color must be a
`var(--token)` (or a `color-mix()` over one). A raw hex value anywhere but
`tokens.json` / `tokens.css` is a bug — the verify test fails on it. This is what
keeps the two themes and the two platforms in step: change a hue once in
`tokens.json`, and web and mobile both follow.

## Token groups

- **color** (theme-aware): `bg`, `surface`, `raise`, `hairline`, `line`, `text`,
  `text-2`, `text-3`, `accent`, `accent-2`, `ok`, `warn`, `crit`, `violet`,
  `cyan`. `hairline` is a translucent divider; `line` is the solid border the Qt
  shell and the Streamlit board draw with.
- **shadow** (theme-aware): `sh`, `sh-hi` — depth tiers, used instead of borders.
- **typography**: `--font-sans`, `--font-mono`; sizes `--fs-*`; weights `--fw-*`;
  tracking `--tracking-*`.
- **spacing**: `--space-*` (4px scale, plus `--space-px`).
- **radii**: `--radius-*`.
- **motion**: `--dur-*`, `--ease-standard`.

## How the desktop web shell consumes them

Import the generated stylesheet, then the primitives, once at the app root:

```css
@import "command_center/design/tokens.css";
@import "command_center/design/primitives.css";
```

`tokens.css` sets the **dark** theme as the default on `:root`, switches to light
under `@media (prefers-color-scheme: light)`, and honors an explicit
`:root[data-theme="dark"|"light"]` that a theme toggle stamps on the root — the
explicit attribute wins over the OS media query in both directions. Shell markup
then uses only primitive classes (`card`, `chip chip--ok`, `nav-item`, `kpi`,
`btn btn--primary`, `input`, `avatar`, …) or references `var(--token)` directly.

## How the mobile app consumes them

Mobile does not load CSS. Its theme adapter reads the same `tokens.json` (parsed
at build time or vendored into a generated theme constant) and maps each token to
the platform primitive:

- `color.*.dark` / `color.*.light` → the two theme palettes.
- `typography`, `space`, `radius`, `motion` → the type ramp, spacing scale, corner
  radii, and animation durations.

Because both platforms derive from `tokens.json`, there is exactly one place to
change a color, and it changes everywhere.

## Regenerate / verify

```bash
python -m command_center.design.build_tokens        # rewrite tokens.css
pytest tests/test_design_tokens.py                  # assert it is in sync + no raw hex
```
