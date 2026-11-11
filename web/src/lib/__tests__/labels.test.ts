import { describe, expect, test } from 'vitest'
import i18n, { en, ru } from '../i18n'
import * as backend from '../__fixtures__/backendValues'
import {
  ATTEMPT_STATE_KEYS,
  formatDurationRu,
  formatOutcomeReason,
  formatRelativeAge,
  humanStatus,
  isStale,
  ATTEMPT_STATE_KEYS as ATTEMPTS,
  KPI_META_KEYS,
  kpiMeta,
  QUEUE_STATE_KEYS,
  reasonLabel,
  VERDICT_KEYS,
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

  test('unknown or empty values become a neutral phrase and keep the raw value', () => {
    expect(humanStatus(RUN_STATE_KEYS, 'WAITING_FOR_APPROVAL', t)).toEqual({ label: 'Статус уточняется', raw: 'WAITING_FOR_APPROVAL' })
    expect(humanStatus(REPO_STATE_KEYS, null, t)).toEqual({ label: 'Статус уточняется', raw: '' })
    expect(runStateKey('nope')).toBe('statusUnclear')
    expect(repoStateKey('ok')).toBe('repoOk')
  })

  test('relativeAge and isStale on a fixed clock', () => {
    expect(relativeAge('2026-11-10T11:30:00Z', NOW).key).toBe('ageNow')
    expect(relativeAge('2026-11-10T07:00:00Z', NOW)).toMatchObject({ key: 'ageHours', hours: 5 })
    expect(relativeAge('2026-11-05T12:00:00Z', NOW)).toMatchObject({ key: 'ageDays', days: 5 })
    expect(relativeAge('garbage', NOW).key).toBeNull()
    expect(relativeAge('2026-11-10T13:00:00Z', NOW).key).toBeNull() // future: never "just now"
    expect(relativeAge('2026-11-10T11:01:00Z', NOW).key).toBe('ageNow')
    expect(relativeAge('2026-11-10T11:00:00Z', NOW)).toMatchObject({ key: 'ageHours', hours: 1 })
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
    expect(formatOutcomeReason('oom', t)).toBe('причина: уточняется')
    expect(formatOutcomeReason('oom', t)).not.toContain('oom')
    expect(formatDurationRu(119.6, t, '-')).toBe('2 мин 0 с')
    expect(formatDurationRu(-5, t, '-')).toBe('0 с')
    expect(priorityLabel('P0', t)).toEqual({ label: 'Критический', title: 'P0' })
    expect(priorityLabel(null, t).label).toBe('Приоритет не задан')
    expect(priorityLabel('P9', t)).toEqual({ label: 'P9', title: 'P9' })
  })

  describe('every backend value has a human label', () => {
    const cases: Array<[string, string[], Record<string, string>]> = [
      ['run state', backend.RUN_STATES, RUN_STATE_KEYS],
      ['queue state', backend.QUEUE_STATES, QUEUE_STATE_KEYS],
      ['attempt state', backend.ATTEMPT_STATES, ATTEMPTS],
      ['repository state', backend.REPOSITORY_STATES, REPO_STATE_KEYS],
      ['verdict', backend.VERDICTS, VERDICT_KEYS],
      ['kpi meta_key', backend.KPI_META_KEYS, KPI_META_KEYS],
    ]
    for (const [name, values, map] of cases) {
      test(name, () => {
        for (const value of values) {
          const key = map[value]
          expect(key, `${name} "${value}" has no label`).toBeTruthy()
          expect((ru as Record<string, string>)[key], `ru text for ${key}`).toBeTruthy()
          expect((en as Record<string, string>)[key], `en text for ${key}`).toBeTruthy()
          if (name !== 'kpi meta_key') expect(humanStatus(map, value, t).label).not.toBe('Статус уточняется')
        }
      })
    }
    test('failure / dead / outcome reasons', () => {
      for (const code of [...backend.REASON_CODES, ...backend.REASON_EXAMPLES_WITH_SUFFIX]) {
        const label = reasonLabel(code, t)
        expect(label, `reason "${code}" has no label`).toBeTruthy()
        expect(label).not.toContain(code)
      }
      expect(reasonLabel('something_new', t)).toBeNull()
    })
  })

  test('unknown KPI meta_key renders nothing, never a raw key', () => {
    expect(kpiMeta('kpi.agents.meta', 3, t)).toBe('')
    expect(kpiMeta('running', 2, t)).toBe('2 активны')
    expect(kpiMeta('all_healthy', undefined, t)).toBe('Все в норме')
  })
})
