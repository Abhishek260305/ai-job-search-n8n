import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { toast } from 'sonner'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { server } from '@/test/server'

// jsdom has no ResizeObserver; Radix measures tooltip arrows with it. Browsers all have it.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// The suite runs on many workers at once; a lazy page or a first render can take longer than the default one second
// under that load, and a slow machine is not a failing test. Passing tests do not wait for this.
configure({ asyncUtilTimeout: 4000 })

// Every request a test makes must be answered by a handler: an unexpected call fails the test.
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  // sonner keeps toasts in a module-wide store and replays the undismissed ones to every new Toaster
  toast.dismiss()
  cleanup()
  server.resetHandlers()
  // the theme lives in localStorage and on <html>, outside what cleanup() resets
  window.localStorage.clear()
  document.documentElement.removeAttribute('class')
})
afterAll(() => server.close())
