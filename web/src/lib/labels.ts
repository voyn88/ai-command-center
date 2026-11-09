import type { TFunction } from 'i18next'

/** Server (owner) mode keeps Russian literals in one place; migration to i18n is package 2. */
export const RU = {
  ownerWaitingTitle: 'Нужно ваше решение',
  ownerWaitingNone: 'Всё под контролем: ваших решений никто не ждёт.',
  ownerWaitingSome: 'Ждут вашего решения: {{n}}. Откройте карточку, чтобы ответить.',
  ownerWaitingViewOnly: 'Ждут решения: {{n}}. Сейчас доступен только просмотр.',
  kpiOpenHint: 'Показать список',
  kpiAllTime: 'всего',
  staleTitle: 'Давно без движения',
  staleNone: 'Застрявших задач нет.',
  staleSome: 'Без движения 3 дня и больше: {{n}}',
  updatedAgo: 'обновлено {{age}}',
  detailsHint: 'Служебные данные задачи',
  detailsNote: 'Готово в списке задач ещё не значит, что изменение уже работает у пользователей.',
  resetFilters: 'Сбросить фильтры',
  retry: 'Повторить',
  loadingTasks: 'Загружаем задачи…',
  errorWithData: 'Не удалось обновить данные. Показан последний полученный список.',
  errorNoData: 'Не удалось загрузить задачи. Проверьте соединение и нажмите «Повторить».',
  priorityNone: 'Приоритет не задан',
} as const

export function fmt(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, k: string) => String(vars[k] ?? ''))
}

export const RUN_STATE_KEYS: Record<string, string> = {
  PREPARED: 'runPrepared',
  QUEUED: 'runQueued',
  RUNNING: 'runRunning',
  COMPLETED: 'runCompleted',
  FAILED: 'runFailed',
  CANCELLED: 'runCancelled',
  INTERRUPTED: 'runInterrupted',
  UNKNOWN: 'runUnknown',
}

export const REPO_STATE_KEYS: Record<string, string> = {
  ok: 'repoOk',
  unconfigured: 'repoUnconfigured',
  invalid_path: 'repoInvalidPath',
  not_git_repo: 'repoNotGit',
}

export const ATTEMPT_STATE_KEYS: Record<string, string> = {
  claimed: 'attemptRunning',
  succeeded: 'attemptDone',
  failed: 'attemptFailed',
  dead: 'attemptFailed',
}

const RUN_STATE_COLOR: Record<string, string> = {
  RUNNING: 'var(--run)',
  QUEUED: 'var(--warn)',
  PENDING: 'var(--warn)',
  PREPARED: 'var(--warn)',
  COMPLETED: 'var(--ok)',
  DONE: 'var(--ok)',
  COMPLETE: 'var(--ok)',
  SUCCESS: 'var(--ok)',
  FAILED: 'var(--bad)',
  ERROR: 'var(--bad)',
  CANCELLED: 'var(--bad)',
  INTERRUPTED: 'var(--bad)',
  UNKNOWN: 'var(--bad)',
}

/** One colour map for every run-state badge (queue, execution). */
export function runStateColor(state: string): string {
  return RUN_STATE_COLOR[(state || '').toUpperCase()] ?? 'var(--tx3-aa)'
}

/** Run states that need the owner's eyes first. */
export const ATTENTION_STATES = ['FAILED', 'INTERRUPTED', 'UNKNOWN']

export const runStateKey = (state: string): string => RUN_STATE_KEYS[(state || '').toUpperCase()] ?? 'unknownState'
export const repoStateKey = (state: string): string => REPO_STATE_KEYS[state] ?? REPO_STATE_KEYS[(state || '').toLowerCase()] ?? 'unknownState'
export const attemptStateKey = (state: string): string => ATTEMPT_STATE_KEYS[(state || '').toLowerCase()] ?? 'unknownState'

/** Human label for a raw status; unknown values -> t('unknownState'). The raw id is returned for a tooltip, never instead of the label. */
export function humanStatus(
  map: Record<string, string>,
  raw: string | null | undefined,
  t: TFunction,
): { label: string; raw: string } {
  const value = raw || ''
  const key = map[value] ?? map[value.toUpperCase()] ?? map[value.toLowerCase()]
  return { label: t(key ?? 'unknownState'), raw: value }
}

const HOUR = 3_600_000

export type RelativeAge = { days: number; hours: number; key: 'ageNow' | 'ageHours' | 'ageDays' | null }

/** Age of an ISO timestamp. Invalid input -> key null (caller shows nothing). */
export function relativeAge(iso: string, now: Date = new Date()): RelativeAge {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return { days: 0, hours: 0, key: null }
  const hours = Math.floor((now.getTime() - then) / HOUR)
  if (hours < 1) return { days: 0, hours: 0, key: 'ageNow' }
  if (hours < 24) return { days: 0, hours, key: 'ageHours' }
  return { days: Math.floor(hours / 24), hours, key: 'ageDays' }
}

export function formatRelativeAge(iso: string, t: TFunction, now?: Date): string | null {
  const age = relativeAge(iso, now)
  if (!age.key) return null
  return t(age.key, { n: age.key === 'ageDays' ? age.days : age.hours })
}

export function isStale(iso: string, days = 3, now: Date = new Date()): boolean {
  return relativeAge(iso, now).days >= days
}

export function formatDurationRu(sec: number | null | undefined, t: TFunction, fallback: string): string {
  if (sec === null || sec === undefined) return fallback
  if (sec < 60) return t('durSec', { n: Math.round(sec) })
  return t('durMin', { m: Math.floor(sec / 60), s: Math.round(sec % 60) })
}

export function priorityLabel(priority: string | null, t: TFunction): { label: string; title?: string } {
  if (!priority) return { label: t('prioNone') }
  const key = { P0: 'prioP0', P1: 'prioP1', P2: 'prioP2', P3: 'prioP3' }[priority]
  return key ? { label: t(key), title: priority } : { label: priority, title: priority }
}

export function formatOutcomeReason(reason: string, t: TFunction): string {
  return reason === 'timeout' ? t('reasonTimeout') : t('reasonGeneric', { reason })
}
