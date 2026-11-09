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
  showAll: 'Показать все',
  showLess: 'Свернуть',
  noTasks: 'Задач пока нет.',
  statusUnclear: 'Статус уточняется',
} as const

export function fmt(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, k: string) => String(vars[k] ?? ''))
}

/** Stale threshold (days) for the owner overview. The RU.staleSome text says "3 дня". */
export const STALE_DAYS = 3

/** Backlog statuses that need no more attention (terminal in the owner's eyes). */
export const BACKLOG_TERMINAL = ['DONE', 'REJECTED', 'DECIDED', 'SPLIT']

export const RUN_STATE_KEYS: Record<string, string> = {
  PREPARED: 'runPrepared',
  QUEUED: 'runQueued',
  RUNNING: 'runRunning',
  COMPLETED: 'runCompleted',
  FAILED: 'runFailed',
  CANCELLED: 'runCancelled',
  INTERRUPTED: 'runInterrupted',
  UNKNOWN: 'runUnknown',
  // Not emitted by the backend today (runtime/db/core.py RUN_STATES); kept so
  // older/foreign sources still read as plain words.
  PENDING: 'runQueued',
  DONE: 'runCompleted',
  COMPLETE: 'runCompleted',
  SUCCESS: 'runCompleted',
  ERROR: 'runFailed',
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
  active: 'attemptRunning',
  failed: 'attemptFailed',
  dead: 'attemptFailed',
  expired: 'attemptExpired',
}

/** work_item.state (db/work_queue_read.py _STATES). */
export const QUEUE_STATE_KEYS: Record<string, string> = {
  ready: 'taskStatusReady',
  claimed: 'taskStatusClaimed',
  succeeded: 'taskStatusSucceeded',
  dead: 'taskStatusDead',
}

/** Report verdicts (models.py VERDICT_LABELS). */
export const VERDICT_KEYS: Record<string, string> = {
  APPROVED_FOR_COMMIT: 'verdictApproved',
  NOT_APPROVED_FOR_COMMIT: 'verdictNotApproved',
  READY_FOR_FINAL_REVIEW: 'verdictReadyReview',
  NOT_READY_FOR_FINAL_REVIEW: 'verdictNotReadyReview',
  READY_FOR_COMMIT: 'verdictReadyCommit',
  FAILED: 'verdictFailed',
}

/** Exact failure/dead/outcome reason codes. */
export const REASON_KEYS: Record<string, string> = {
  timeout: 'reasonTimeout',
  supervision_failed: 'reasonSupervision',
  launch_preparation_failed: 'reasonLaunchPrep',
  launch_setup_failed: 'reasonLaunchSetup',
  executable_missing: 'reasonExecutableMissing',
  provider_launch_failed: 'reasonProviderLaunch',
  session_expired: 'reasonSessionExpired',
  authentication_failed: 'reasonAuthFailed',
  quota_limit: 'reasonQuota',
  provider_api_error: 'reasonProviderApi',
  provider_exit_nonzero: 'reasonExitNonzero',
  daemon_unreachable: 'reasonDaemon',
  model_missing: 'reasonModelMissing',
  insufficient_memory: 'reasonMemory',
  visibility_timeout: 'reasonVisibility',
  visibility_timeout_exhausted: 'reasonAttemptsOut',
  attempt_budget_exhausted: 'reasonAttemptsOut',
}

/** Reason families that carry a free-text suffix after the colon. */
export const REASON_PREFIX_KEYS: Record<string, string> = {
  'capability_mismatch:': 'reasonCapability',
  'lease_wait_exhausted:': 'reasonLeaseWait',
  'infra_wait_exhausted:': 'reasonInfraWait',
  'blocked:': 'reasonBlocked',
  'incomplete:': 'reasonIncomplete',
}

/** Backend KPI meta_key values (webapi/serializers.py _kpis) -> i18n key. */
export const KPI_META_KEYS: Record<string, string> = {
  all_healthy: 'all_healthy',
  running: 'running',
  in_progress: 'in_progress',
  pending: 'pending',
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

export const runStateKey = (state: string): string => RUN_STATE_KEYS[(state || '').toUpperCase()] ?? 'statusUnclear'
export const repoStateKey = (state: string): string => REPO_STATE_KEYS[state] ?? REPO_STATE_KEYS[(state || '').toLowerCase()] ?? 'statusUnclear'
export const attemptStateKey = (state: string): string => ATTEMPT_STATE_KEYS[(state || '').toLowerCase()] ?? 'statusUnclear'

/** Human label for a raw status; unknown values -> t('statusUnclear'). The raw id is returned for a tooltip, never instead of the label. */
export function humanStatus(
  map: Record<string, string>,
  raw: string | null | undefined,
  t: TFunction,
): { label: string; raw: string } {
  const value = raw || ''
  const key = map[value] ?? map[value.toUpperCase()] ?? map[value.toLowerCase()]
  return { label: t(key ?? 'statusUnclear'), raw: value }
}

const HOUR = 3_600_000

export type RelativeAge = { days: number; hours: number; key: 'ageNow' | 'ageHours' | 'ageDays' | null }

/** Age of an ISO timestamp. Invalid input -> key null (caller shows nothing). */
export function relativeAge(iso: string, now: Date = new Date()): RelativeAge {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return { days: 0, hours: 0, key: null }
  const diff = now.getTime() - then
  // A timestamp from the future (clock skew) must not read as "just now".
  if (diff < 0) return { days: 0, hours: 0, key: null }
  const hours = Math.floor(diff / HOUR)
  if (hours < 1) return { days: 0, hours: 0, key: 'ageNow' }
  if (hours < 24) return { days: 0, hours, key: 'ageHours' }
  return { days: Math.floor(hours / 24), hours, key: 'ageDays' }
}

export function formatRelativeAge(iso: string, t: TFunction, now?: Date): string | null {
  const age = relativeAge(iso, now)
  if (!age.key) return null
  return t(age.key, { n: age.key === 'ageDays' ? age.days : age.hours })
}

export function isStale(iso: string, days = STALE_DAYS, now: Date = new Date()): boolean {
  return relativeAge(iso, now).days >= days
}

export function formatDurationRu(sec: number | null | undefined, t: TFunction, fallback: string): string {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return fallback
  const total = Math.max(0, Math.round(sec))
  if (total < 60) return t('durSec', { n: total })
  return t('durMin', { m: Math.floor(total / 60), s: total % 60 })
}

export function priorityLabel(priority: string | null, t: TFunction): { label: string; title?: string } {
  if (!priority) return { label: t('prioNone') }
  const key = { P0: 'prioP0', P1: 'prioP1', P2: 'prioP2', P3: 'prioP3' }[priority]
  return key ? { label: t(key), title: priority } : { label: priority, title: priority }
}

/** Known reason -> plain phrase; null when the code is not in the dictionary. */
export function reasonLabel(reason: string | null | undefined, t: TFunction): string | null {
  const value = (reason || '').trim()
  if (!value) return null
  const exact = REASON_KEYS[value]
  if (exact) return t(exact)
  const prefix = Object.keys(REASON_PREFIX_KEYS).find((p) => value.startsWith(p))
  return prefix ? t(REASON_PREFIX_KEYS[prefix]) : null
}

/** Reason for inline use: known phrase, otherwise a neutral one (raw code goes to a tooltip). */
export function formatOutcomeReason(reason: string, t: TFunction): string {
  return reasonLabel(reason, t) ?? t('reasonGeneric', { reason: t('reasonUnclear') })
}

/** KPI card second line. Unknown backend meta_key renders nothing, never the raw key. */
export function kpiMeta(key: string, n: number | undefined, t: TFunction): string {
  const i18nKey = KPI_META_KEYS[key]
  if (!i18nKey) return ''
  return n !== undefined ? `${n} ${t(i18nKey)}` : t(i18nKey)
}
