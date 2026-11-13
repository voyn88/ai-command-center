// `@testing-library/jest-dom` v7's base entrypoint calls `expect.extend(...)`
// against a Jest-style GLOBAL `expect`, which does not exist here because
// `test.globals` is not enabled in vite.config.ts (it would throw
// "ReferenceError: expect is not defined" at setup time). The `/vitest`
// subpath instead imports `expect` from 'vitest' explicitly and extends
// that, which works regardless of the `globals` setting.
import '@testing-library/jest-dom/vitest'

// `test.globals` is not enabled (see above), so `@testing-library/react`'s
// automatic-cleanup detection (which looks for a global `afterEach`) never
// fires. Without this, the DOM from one test's `render()` leaks into the
// next test in the same file. Register it explicitly instead.
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
})

// The app now defaults to Russian; existing screen tests assert English
// strings, so pin every test to English unless it switches language itself.
import { beforeEach } from 'vitest'
import i18n from './lib/i18n'

beforeEach(async () => {
  await i18n.changeLanguage('en')
})
