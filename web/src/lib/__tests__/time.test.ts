import { describe, test, expect } from 'vitest'
import { formatAppTimestamp, parseAppTimestamp } from '../time'

describe('parseAppTimestamp', () => {
  test('an offsetless API timestamp is read as UTC, not as browser-local', () => {
    // What `models.iso_now()` writes. `new Date(value)` alone would read this
    // in the browser's zone and land on a different instant everywhere except
    // UTC — the bug this helper exists to prevent.
    const parsed = parseAppTimestamp('2026-09-15T03:10:00')

    expect(parsed).not.toBeNull()
    expect(parsed!.toISOString()).toBe('2026-09-15T03:10:00.000Z')
  })

  test('a timestamp that carries its own offset keeps it', () => {
    // Not every store in the app writes naive values; an aware one is already
    // unambiguous and must not be re-stamped.
    expect(parseAppTimestamp('2026-09-15T03:10:00+05:00')!.toISOString()).toBe(
      '2026-09-14T22:10:00.000Z',
    )
    expect(parseAppTimestamp('2026-09-15T03:10:00Z')!.toISOString()).toBe(
      '2026-09-15T03:10:00.000Z',
    )
  })

  test('missing or unparseable input is null rather than an Invalid Date', () => {
    expect(parseAppTimestamp(null)).toBeNull()
    expect(parseAppTimestamp(undefined)).toBeNull()
    expect(parseAppTimestamp('   ')).toBeNull()
    expect(parseAppTimestamp('не дата')).toBeNull()
  })

  test('ordering follows the UTC scale the values are written on', () => {
    // The point of the scale change (VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL): two
    // stamps from either side of a DST fall-back sort chronologically.
    const earlier = parseAppTimestamp('2026-11-01T08:50:00')!
    const later = parseAppTimestamp('2026-11-01T09:10:00')!

    expect(earlier.getTime()).toBeLessThan(later.getTime())
  })
})

describe('formatAppTimestamp', () => {
  test('a missing value renders the fallback', () => {
    expect(formatAppTimestamp(null, 'ru', '—')).toBe('—')
  })

  test('an unparseable value is shown as-is rather than as "Invalid Date"', () => {
    expect(formatAppTimestamp('не дата', 'ru', '—')).toBe('не дата')
  })

  test('a real timestamp renders in the reader’s own locale and zone', () => {
    const rendered = formatAppTimestamp('2026-09-15T03:10:00', 'ru', '—')

    expect(rendered).not.toBe('—')
    expect(rendered).toContain('2026')
  })
})
