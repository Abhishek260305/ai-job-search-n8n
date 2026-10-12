import { useSyncExternalStore } from 'react'

/** Phones and narrow windows: the jobs list becomes cards instead of a nine-column table (Tailwind's `md` is 768px). */
export const NARROW = '(max-width: 767px)'

/**
 * Whether a CSS media query matches now, and again whenever it changes (rotating a phone, resizing a window).
 * Where the browser cannot answer (tests, very old engines) it is false, which is the desktop layout.
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
      const list = window.matchMedia(query)
      list.addEventListener('change', notify)
      return () => list.removeEventListener('change', notify)
    },
    () => (typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(query).matches : false),
    () => false,
  )
}
