import { afterEach, describe, test, expect, vi } from 'vitest'
import i18n, { en, ru, initialLang, LANG_KEY, toggleLang } from '../i18n'

function fakeStorage(initial: Record<string, string> = {}) {
  const data = { ...initial }
  return {
    data,
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => { data[k] = v },
    removeItem: (k: string) => { delete data[k] },
  }
}
function useStorage(storage: ReturnType<typeof fakeStorage> | null) {
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get: () => { if (!storage) throw new Error('blocked'); return storage },
  })
}

afterEach(() => {
  vi.resetModules()
})

describe('i18n', () => {
  test('EN and RU have identical keys', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(ru).sort())
  })

  test('default language on a clean browser is Russian', async () => {
    useStorage(fakeStorage())
    expect(initialLang()).toBe('ru')
    vi.resetModules()
    const fresh = (await import('../i18n')).default
    expect(fresh.language).toBe('ru')
    expect(document.documentElement.lang).toBe('ru')
  })

  test('invalid or unreadable saved language falls back to Russian', () => {
    useStorage(fakeStorage({ [LANG_KEY]: 'de' }))
    expect(initialLang()).toBe('ru')
    useStorage(null)
    expect(initialLang()).toBe('ru')
  })

  test('the chosen language is saved and restored after a reload', async () => {
    const storage = fakeStorage()
    useStorage(storage)
    await i18n.changeLanguage('ru')
    toggleLang()
    expect(storage.data[LANG_KEY]).toBe('en')
    expect(document.documentElement.lang).toBe('en')
    vi.resetModules()
    const fresh = (await import('../i18n')).default
    expect(fresh.language).toBe('en')
  })
})
