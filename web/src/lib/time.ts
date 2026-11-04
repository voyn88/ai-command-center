// Reading the API's timestamps.
//
// `command_center.models.iso_now()` writes naive UTC with no offset
// ("2026-09-15T03:10:00") — see that function for why the scale is UTC
// (`VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL`) and why the *format* deliberately did
// not change. ECMAScript parses a date-time string with no offset as **local**
// time, so `new Date(run.created_at)` reads every API timestamp in the
// browser's zone and displays it shifted by that zone's UTC offset.
//
// One helper, used by every screen that renders one of these, so the reading
// is stated once instead of per component.

/** True when an ISO-8601 string already carries a zone (`Z` or `±HH:MM`). */
function hasZone(value: string): boolean {
  return /(?:Z|[+-]\d{2}:?\d{2})$/.test(value.trim())
}

/**
 * Parse a timestamp from this app's API into a `Date`.
 *
 * An offsetless value is read as UTC, which is what the API writes; a value
 * that carries its own offset (some stores write aware timestamps) is left
 * alone. Returns `null` for empty or unparseable input, so a caller can fall
 * back to a placeholder instead of rendering "Invalid Date".
 */
export function parseAppTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null
  const text = value.trim()
  if (!text) return null
  const date = new Date(hasZone(text) ? text : `${text}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

/**
 * An API timestamp as a localized absolute date-time, or `fallback` when it is
 * missing/unparseable. The rendering is in the reader's own zone — the point of
 * parsing the UTC scale correctly first.
 */
export function formatAppTimestamp(
  value: string | null | undefined,
  language: string,
  fallback: string,
): string {
  const date = parseAppTimestamp(value)
  if (date === null) return value ? value : fallback
  return new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}
