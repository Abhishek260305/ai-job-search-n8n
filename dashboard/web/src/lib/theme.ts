import { useSyncExternalStore } from 'react'

export type Theme = 'dark' | 'light'

/** Also read by public/theme.js, which applies a saved light theme before the first paint. */
export const THEME_KEY = 'jd-theme'
export const DEFAULT_THEME: Theme = 'dark'

const listeners = new Set<() => void>()
// the choice for this tab when the browser refused to save it (blocked cookies, some private modes)
let unsaved: Theme | null = null

function saved(): string | null {
  if (unsaved) return unsaved
  try {
    return window.localStorage.getItem(THEME_KEY)
  } catch {
    return null
  }
}

/** The saved theme; anything else, including nothing saved yet, is the default (dark). */
export function currentTheme(): Theme {
  const value = saved()
  return value === 'light' || value === 'dark' ? value : DEFAULT_THEME
}

/** The `dark` class on <html> switches every colour token (src/index.css). */
export function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

export function setTheme(theme: Theme) {
  try {
    window.localStorage.setItem(THEME_KEY, theme)
    unsaved = null
  } catch {
    unsaved = theme   // this tab keeps it until it closes
  }
  applyTheme(theme)
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  // another tab changed it
  const onStorage = (event: StorageEvent) => {
    if (event.key !== THEME_KEY && event.key !== null) return
    applyTheme(currentTheme())
    listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, currentTheme)
}
