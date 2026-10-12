import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it } from 'vitest'
import { applyTheme, currentTheme, THEME_KEY } from '@/lib/theme'
import { renderApp } from '@/test/render'

const isDark = () => document.documentElement.classList.contains('dark')

// as main.tsx does at startup
beforeEach(() => applyTheme(currentTheme()))

describe('the dark mode switch', () => {
  it('is on by default and switches the whole page to light and back, remembering the choice', async () => {
    renderApp('/')
    const toggle = await screen.findByRole('button', { name: 'Dark mode' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(toggle).toHaveAttribute('title', 'Switch to light mode')
    expect(isDark()).toBe(true)

    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    expect(toggle).toHaveAttribute('title', 'Switch to dark mode')
    expect(isDark()).toBe(false)
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')

    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(isDark()).toBe(true)
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
  })

  it('starts light when light was chosen before', async () => {
    window.localStorage.setItem(THEME_KEY, 'light')
    applyTheme(currentTheme())
    renderApp('/')
    expect(await screen.findByRole('button', { name: 'Dark mode' })).toHaveAttribute('aria-pressed', 'false')
    expect(isDark()).toBe(false)
  })

  it('is on the login page too, after the form', async () => {
    renderApp('/login')
    const password = await screen.findByLabelText('Password')
    expect(password).toHaveFocus()
    await userEvent.type(password, 'x')   // enables Sign in, the next stop
    await userEvent.tab()
    expect(screen.getByRole('button', { name: 'Sign in' })).toHaveFocus()
    await userEvent.tab()
    expect(screen.getByRole('button', { name: 'Dark mode' })).toHaveFocus()
    await userEvent.click(screen.getByRole('button', { name: 'Dark mode' }))
    expect(isDark()).toBe(false)
  })

  it('takes the notifications along', async () => {
    renderApp('/')
    const toggle = await screen.findByRole('button', { name: 'Dark mode' })
    toast('Saved')
    await screen.findByText('Saved')
    const toaster = () => document.querySelector('[data-sonner-toaster]')
    expect(toaster()).toHaveAttribute('data-sonner-theme', 'dark')
    await userEvent.click(toggle)
    expect(toaster()).toHaveAttribute('data-sonner-theme', 'light')
  })
})
