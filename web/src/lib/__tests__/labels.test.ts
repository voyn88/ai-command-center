import { describe, expect, test } from 'vitest'
import i18n from '../i18n'
import {
  ATTEMPT_STATE_KEYS,
  formatDurationRu,
  formatOutcomeReason,
  formatRelativeAge,
  humanStatus,
  isStale,
  priorityLabel,
  relativeAge,
  REPO_STATE_KEYS,
  repoStateKey,
  runStateKey,
  RUN_STATE_KEYS,
} from '../labels'

const NOW = new Date('2026-11-10T12:00:00Z')

describe('labels', () => {
  const t = i18n.getFixedT('ru')

  test('known run states map to the exact Russian texts, raw id returned separately', () => {
    expect(humanStatus(RUN_STATE_KEYS, 'RUNNING', t)).toEqual({ label: 'Идёт сейчас', raw: 'RUNNING' })
    expect(humanStatus(RUN_STATE_KEYS, 'FAILED', t).label).toBe('Сбой')
    expect(humanStatus(REPO_STATE_KEYS, 'not_git_repo', t).label).toBe('Папка не является git-репозиторием')
    expect(humanStatus(ATTEMPT_STATE_KEYS, 'dead', t).label).toBe('не удалось')
  })

  test('unknown or empty values become "Неизвестно" and keep the raw value', () => {
    expect(humanStatus(RUN_STATE_KEYS, 'WEIRD', t)).toEqual({ label: 'Неизвестно', raw: 'WEIRD' })
    expect(humanStatus(REPO_STATE_KEYS, null, t)).toEqual({ label: 'Неизвестно', raw: '' })
    expect(runStateKey('nope')).toBe('unknownState')
    expect(repoStateKey('ok')).toBe('repoOk')
  })

  test('relativeAge and isStale on a fixed clock', () => {
    expect(relativeAge('2026-11-10T11:30:00Z', NOW).key).toBe('ageNow')
    expect(relativeAge('2026-11-10T07:00:00Z', NOW)).toMatchObject({ key: 'ageHours', hours: 5 })
    expect(relativeAge('2026-11-05T12:00:00Z', NOW)).toMatchObject({ key: 'ageDays', days: 5 })
    expect(relativeAge('garbage', NOW).key).toBeNull()
    expect(isStale('2026-11-07T12:00:00Z', 3, NOW)).toBe(true)
    expect(isStale('2026-11-08T12:00:00Z', 3, NOW)).toBe(false)
    expect(isStale('garbage', 3, NOW)).toBe(false)
    expect(formatRelativeAge('2026-11-05T12:00:00Z', t, NOW)).toBe('5 дн. назад')
    expect(formatRelativeAge('2026-11-10T07:00:00Z', t, NOW)).toBe('5 ч назад')
  })

  test('durations, reasons and priorities are human', () => {
    expect(formatDurationRu(42, t, '-')).toBe('42 с')
    expect(formatDurationRu(303, t, '-')).toBe('5 мин 3 с')
    expect(formatDurationRu(null, t, 'нет')).toBe('нет')
    expect(formatOutcomeReason('timeout', t)).toBe('не уложились во время')
    expect(formatOutcomeReason('oom', t)).toBe('причина: oom')
    expect(priorityLabel('P0', t)).toEqual({ label: 'Критический', title: 'P0' })
    expect(priorityLabel(null, t).label).toBe('Приоритет не задан')
    expect(priorityLabel('P9', t)).toEqual({ label: 'P9', title: 'P9' })
  })
})
