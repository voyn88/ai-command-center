// Dictation for backlog intake (VOYN-W0-APP-CONTROL-S6b).
//
// Transcription happens on the device, through the Web Speech API the owner's
// iPhone and Mac already ship: nothing to install on control-01, no audio
// leaves the device, and the PWA (S3) gets voice with no native shell. The
// term repair — the part that is actually hard, because a recognizer has
// never heard of `VOYN-…` or `P0` — is deliberately NOT here: it lives
// server-side in `command_center/db/voice_transcript.py`, so a later
// server-side Whisper path can replace this file alone.
//
// Browsers without the API (Firefox, a locked-down WebView) are a supported
// case, not an error: `startDictation` returns null and the caller keeps the
// typed-text path it already had.

export type DictationError = 'denied' | 'no-speech' | 'network' | 'failed'

export type DictationHandle = {
  /** Stop listening; `onEnd` still fires, and text heard so far is kept. */
  stop: () => void
}

type SpeechRecognitionLike = {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  start: () => void
  stop: () => void
  abort: () => void
  onresult: ((event: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
}

type RecognitionConstructor = new () => SpeechRecognitionLike

function recognitionConstructor(): RecognitionConstructor | null {
  if (typeof window === 'undefined') return null
  const scope = window as unknown as {
    SpeechRecognition?: RecognitionConstructor
    webkitSpeechRecognition?: RecognitionConstructor
  }
  // Safari (iOS included) exposes only the prefixed name.
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null
}

/** Whether this browser can dictate at all — checked before the mic button
 * is rendered, so an unsupported browser is never offered a dead control. */
export function dictationSupported(): boolean {
  return recognitionConstructor() !== null
}

function mapError(code: string): DictationError {
  if (code === 'not-allowed' || code === 'service-not-allowed') return 'denied'
  if (code === 'no-speech') return 'no-speech'
  if (code === 'network') return 'network'
  return 'failed'
}

/** Start one dictation turn.
 *
 * `onText` receives the transcript so far on every update (interim results
 * included) — the owner watches the words land in the same box they would
 * have typed into, and can edit them before drafting. Returns null when the
 * browser has no recognizer, and a handle otherwise; `onEnd` always fires
 * exactly once, whether the turn ended by silence, by `stop()` or by error.
 */
export function startDictation(options: {
  lang: string
  onText: (text: string) => void
  onError: (error: DictationError) => void
  onEnd: () => void
}): DictationHandle | null {
  const Recognition = recognitionConstructor()
  if (!Recognition) return null

  const recognition = new Recognition()
  recognition.lang = options.lang
  // One utterance per press: a continuous stream on a phone in a pocket is a
  // way to file a task nobody dictated.
  recognition.continuous = false
  recognition.interimResults = true
  recognition.maxAlternatives = 1

  let settled = false
  const settle = () => {
    if (settled) return
    settled = true
    options.onEnd()
  }

  recognition.onresult = (event) => {
    let transcript = ''
    for (let index = 0; index < event.results.length; index += 1) {
      const alternative = event.results[index]?.[0]
      if (alternative) transcript += alternative.transcript
    }
    if (transcript.trim()) options.onText(transcript.trim())
  }
  recognition.onerror = (event) => {
    options.onError(mapError(event.error))
    settle()
  }
  recognition.onend = settle

  try {
    recognition.start()
  } catch {
    // start() throws if a turn is already running in this tab.
    options.onError('failed')
    settle()
    return null
  }

  return {
    stop: () => {
      try {
        recognition.stop()
      } catch {
        settle()
      }
    },
  }
}
