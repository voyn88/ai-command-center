import { describe, expect, test } from 'vitest'
import { BACKGROUND_PRESETS } from '../backgroundPresets'

// Read the real stylesheet (vitest runs from web/) (vitest swallows CSS imports, even ?raw/?inline).
// Node types are not part of the app tsconfig, hence the suppressions.
// @ts-expect-error node:fs has no types here
import { readFileSync } from 'node:fs'
const tokens: string = readFileSync('src/theme/tokens.css', 'utf8')

type RGB = [number, number, number]

const hex = (s: string): RGB => [1, 3, 5].map((i) => parseInt(s.slice(i, i + 2), 16)) as RGB
const token = (name: string) => {
  const m = tokens.match(new RegExp(`--${name}:\\s*([^;]+);`))
  if (!m) throw new Error(`token --${name} not found`)
  return m[1].trim()
}
const parseColor = (v: string): { rgb: RGB; a: number } => {
  if (v.startsWith('#')) return { rgb: hex(v), a: 1 }
  const m = v.match(/rgba?\(([^)]+)\)/)
  if (!m) throw new Error(`cannot parse ${v}`)
  const [r, g, b, a = '1'] = m[1].split(',').map((x) => x.trim())
  return { rgb: [Number(r), Number(g), Number(b)], a: Number(a) }
}
const channel = (v: number) => {
  const c = v / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}
const lum = ([r, g, b]: RGB) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
const ratio = (a: RGB, b: RGB) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
const over = (fg: RGB, a: number, bg: RGB): RGB => [0, 1, 2].map((i) => fg[i] * a + bg[i] * (1 - a)) as RGB

/** Worst case for light text: the LIGHTEST solid stop of the preset, lit further by its strongest overlay. */
function lightestBackground(value: string): RGB {
  const stops = (value.match(/#[0-9a-fA-F]{6}/g) || []).map(hex).sort((x, y) => lum(y) - lum(x))
  let bg = stops[0]
  const overlays = [...value.matchAll(/rgba\((\d+),\s*(\d+),\s*(\d+),\s*([.\d]+)\)/g)].map((m) => ({ rgb: [Number(m[1]), Number(m[2]), Number(m[3])] as RGB, a: Number(m[4]) }))
  const strongest = overlays.sort((x, y) => lum(y.rgb) * y.a - lum(x.rgb) * x.a)[0]
  if (strongest) bg = over(strongest.rgb, strongest.a, bg)
  return bg
}

describe('secondary text contrast (WCAG AA 4.5:1) on every background preset', () => {
  const glass = parseColor(token('glass'))
  for (const textToken of ['tx3-aa', 'tx2']) {
    const text = parseColor(token(textToken)).rgb
    for (const preset of BACKGROUND_PRESETS) {
      test(`--${textToken} on glass over "${preset.id}"`, () => {
        const surface = over(glass.rgb, glass.a, lightestBackground(preset.value))
        const r = ratio(text, surface)
        expect(r, `${textToken}/${preset.id} = ${r.toFixed(2)}`).toBeGreaterThanOrEqual(4.5)
      })
    }
  }
  test('covers all presets', () => {
    expect(BACKGROUND_PRESETS.length).toBe(7)
  })
})
