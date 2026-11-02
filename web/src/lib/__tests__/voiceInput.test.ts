// Dictation wrapper (VOYN-W0-APP-CONTROL-S6b). jsdom has no Web Speech API,
// so these tests install a recognizer double on `window` and drive its
// callbacks — which is also the shape of the two browsers that matter:
// Safari/iOS exposes only `webkitSpeechRecognition`, Firefox exposes neither.

import { afterEach, describe, expect, test, vi } from 'vitest'
import { dictationSupported, startDictation } from '../voiceInput'

type Handlers = {
  onresult: ((event: unknown) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
}

class FakeRecognition implements Handlers {
  static last: FakeRecognition | null = null
  static failOnStart = false

  lang = ''
  continuous = false
  interimResults = false
  maxAlternatives = 0
  started = false
  stopped = false
  onresult: ((event: unknown) => void) | null = null
  onerror: ((event: { error: string }) => void) | null = null
  onend: (() => void) | null = null

  constructor() {
    FakeRecognition.last = this
  }

  start() {
    if (FakeRecognition.failOnStart) throw new Error('already started')
    this.started = true
  }

  stop() {
    this.stopped = true
  }

  abort() {}

  /** Feed a transcript the way the browser does: an indexed result list. */
  emit(chunks: { transcript: string; isFinal: boolean }[]) {
    const results = chunks.map((chunk) => {
      const entry = [{ transcript: chunk.transcript }] as unknown as ArrayLike<{ transcript: string }> & {
        isFinal: boolean
      }
      entry.isFinal = chunk.isFinal
      return entry
    })
    this.onresult?.({ resultIndex: 0, results })
  }
}

function install(name: 'SpeechRecognition' | 'webkitSpeechRecognition') {
  ;(window as unknown as Record<string, unknown>)[name] = FakeRecognition
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).SpeechRecognition
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition
  FakeRecognition.last = null
  FakeRecognition.failOnStart = false
})

describe('dictationSupported', () => {
  test('is false in a browser with no recognizer', () => {
    expect(dictationSupported()).toBe(false)
  })

  test('accepts the prefixed Safari/iOS name', () => {
    install('webkitSpeechRecognition')
    expect(dictationSupported()).toBe(true)
  })

  test('accepts the unprefixed name', () => {
    install('SpeechRecognition')
    expect(dictationSupported()).toBe(true)
  })
})

describe('startDictation', () => {
  test('returns null instead of a dead handle when unsupported', () => {
    const onError = vi.fn()
    expect(
      startDictation({ lang: 'ru-RU', onText: vi.fn(), onError, onEnd: vi.fn() }),
    ).toBeNull()
    expect(onError).not.toHaveBeenCalled()
  })

  test('listens for one utterance in the requested language', () => {
    install('webkitSpeechRecognition')
    startDictation({ lang: 'ru-RU', onText: vi.fn(), onError: vi.fn(), onEnd: vi.fn() })

    const recognition = FakeRecognition.last!
    expect(recognition.started).toBe(true)
    expect(recognition.lang).toBe('ru-RU')
    // one press, one utterance — a continuous stream would file tasks nobody dictated
    expect(recognition.continuous).toBe(false)
    expect(recognition.interimResults).toBe(true)
  })

  test('reports the running transcript, interim words included', () => {
    install('SpeechRecognition')
    const onText = vi.fn()
    startDictation({ lang: 'en-US', onText, onError: vi.fn(), onEnd: vi.fn() })

    FakeRecognition.last!.emit([
      { transcript: 'add a task', isFinal: true },
      { transcript: ' wave 0', isFinal: false },
    ])

    expect(onText).toHaveBeenCalledWith('add a task wave 0')
  })

  test('ignores an empty result rather than clearing what was heard', () => {
    install('SpeechRecognition')
    const onText = vi.fn()
    startDictation({ lang: 'en-US', onText, onError: vi.fn(), onEnd: vi.fn() })

    FakeRecognition.last!.emit([{ transcript: '   ', isFinal: false }])

    expect(onText).not.toHaveBeenCalled()
  })

  test('maps a refused microphone to a permission error and ends the turn', () => {
    install('SpeechRecognition')
    const onError = vi.fn()
    const onEnd = vi.fn()
    startDictation({ lang: 'en-US', onText: vi.fn(), onError, onEnd })

    FakeRecognition.last!.onerror?.({ error: 'not-allowed' })

    expect(onError).toHaveBeenCalledWith('denied')
    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  test('maps silence and offline recognition to their own messages', () => {
    install('SpeechRecognition')
    const first = vi.fn()
    startDictation({ lang: 'en-US', onText: vi.fn(), onError: first, onEnd: vi.fn() })
    FakeRecognition.last!.onerror?.({ error: 'no-speech' })
    expect(first).toHaveBeenCalledWith('no-speech')

    const second = vi.fn()
    startDictation({ lang: 'en-US', onText: vi.fn(), onError: second, onEnd: vi.fn() })
    FakeRecognition.last!.onerror?.({ error: 'network' })
    expect(second).toHaveBeenCalledWith('network')

    const third = vi.fn()
    startDictation({ lang: 'en-US', onText: vi.fn(), onError: third, onEnd: vi.fn() })
    FakeRecognition.last!.onerror?.({ error: 'audio-capture' })
    expect(third).toHaveBeenCalledWith('failed')
  })

  test('ends the turn exactly once when an error is followed by onend', () => {
    install('SpeechRecognition')
    const onEnd = vi.fn()
    startDictation({ lang: 'en-US', onText: vi.fn(), onError: vi.fn(), onEnd })

    FakeRecognition.last!.onerror?.({ error: 'network' })
    FakeRecognition.last!.onend?.()

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  test('stop() asks the recognizer to finish', () => {
    install('SpeechRecognition')
    const handle = startDictation({
      lang: 'en-US',
      onText: vi.fn(),
      onError: vi.fn(),
      onEnd: vi.fn(),
    })

    handle!.stop()

    expect(FakeRecognition.last!.stopped).toBe(true)
  })

  test('a recognizer that refuses to start reports an error, not a handle', () => {
    install('SpeechRecognition')
    FakeRecognition.failOnStart = true
    const onError = vi.fn()
    const onEnd = vi.fn()

    const handle = startDictation({ lang: 'en-US', onText: vi.fn(), onError, onEnd })

    expect(handle).toBeNull()
    expect(onError).toHaveBeenCalledWith('failed')
    expect(onEnd).toHaveBeenCalledTimes(1)
  })
})
