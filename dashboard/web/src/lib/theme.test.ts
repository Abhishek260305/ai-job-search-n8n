import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import indexHtml from '../../index.html?raw'
import themeScript from '../../public/theme.js?raw'
import { applyTheme, currentTheme, DEFAULT_THEME, setTheme, THEME_KEY, useTheme } from '@/lib/theme'

const html = document.documentElement
const isDark = () => html.classList.contains('dark')

afterEach(() => {
  vi.restoreAllMocks()
  setTheme('dark')   // forgets a choice kept in memory by the blocked-storage test
})

describe('the saved theme', () => {
  it('is dark until something else is chosen', () => {
    expect(DEFAULT_THEME).toBe('dark')
    expect(currentTheme()).toBe('dark')
    window.localStorage.setItem(THEME_KEY, 'blue')
    expect(currentTheme()).toBe('dark')
    window.localStorage.setItem(THEME_KEY, 'light')
    expect(currentTheme()).toBe('light')
  })

  it('is saved and applied to <html> when chosen', () => {
    setTheme('light')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')
    expect(isDark()).toBe(false)
    setTheme('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(isDark()).toBe(true)
    applyTheme('light')
    expect(isDark()).toBe(false)
  })

  it('re-renders whoever uses it', () => {
    const { result } = renderHook(() => useTheme())
    expect(result.current).toBe('dark')
    act(() => setTheme('light'))
    expect(result.current).toBe('light')
  })

  it('follows a change made in another tab', () => {
    const { result } = renderHook(() => useTheme())
    applyTheme('dark')
    window.localStorage.setItem(THEME_KEY, 'light')
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'something-else' })) })
    expect(result.current).toBe('dark')
    expect(isDark()).toBe(true)
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: THEME_KEY })) })
    expect(result.current).toBe('light')
    expect(isDark()).toBe(false)
  })

  it('still switches for this tab when the browser refuses storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('denied', 'SecurityError') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('denied', 'SecurityError') })
    expect(currentTheme()).toBe('dark')
    setTheme('light')
    expect(currentTheme()).toBe('light')
    expect(isDark()).toBe(false)
  })
})

describe('before the first paint', () => {
  const run = () => new Function(themeScript)()

  it('index.html starts dark and loads theme.js in <head>, as a classic script', () => {
    expect(indexHtml).toMatch(/<html lang="en" class="dark">/)
    const head = indexHtml.slice(0, indexHtml.indexOf('</head>'))
    expect(head).toContain('<script src="/theme.js"></script>')
    expect(themeScript).toContain(`'${THEME_KEY}'`)
  })

  it('removes the dark class only for a saved light theme', () => {
    applyTheme('dark')
    run()
    expect(isDark()).toBe(true)
    window.localStorage.setItem(THEME_KEY, 'dark')
    run()
    expect(isDark()).toBe(true)
    window.localStorage.setItem(THEME_KEY, 'light')
    run()
    expect(isDark()).toBe(false)
  })

  it('stays dark without throwing when storage is blocked', () => {
    applyTheme('dark')
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('denied', 'SecurityError') })
    expect(run).not.toThrow()
    expect(isDark()).toBe(true)
  })
})
