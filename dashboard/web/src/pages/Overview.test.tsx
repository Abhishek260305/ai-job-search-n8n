import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { APP_VERSION } from '@/lib/appInfo'
import { locationOf, renderApp } from '@/test/render'

const search = () => screen.getByRole('searchbox', { name: 'Search title or company' })

async function loaded(path = '/') {
  const app = renderApp(path)
  await screen.findByRole('table', { hidden: true })
  return app
}

describe('Overview: keyboard', () => {
  it('"/" jumps to the search and selects what is there, without typing a slash', async () => {
    await loaded('/?q=acme')
    expect(search()).toHaveAttribute('aria-keyshortcuts', '/')
    await userEvent.keyboard('/')
    expect(search()).toHaveFocus()
    expect(search()).toHaveValue('acme')
    const box = search() as HTMLInputElement
    expect([box.selectionStart, box.selectionEnd]).toEqual([0, 4])
  })

  it('"/" is a character while typing in a field', async () => {
    const user = userEvent.setup()
    await loaded()
    await user.click(search())
    await user.keyboard('c/c++')
    expect(search()).toHaveValue('c/c++')
  })

  it('"/" does nothing with a modifier, or while a panel or menu has the keyboard', async () => {
    const user = userEvent.setup()
    await loaded('/?job=1')
    await screen.findByRole('dialog', { name: 'Engineer 1' })
    await user.keyboard('/')
    expect(document.activeElement).not.toBe(screen.getByRole('searchbox', { hidden: true }))
    // and the key is left alone (not swallowed) while the panel is open
    expect(fireEvent.keyDown(document.activeElement!, { key: '/' })).toBe(true)
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => search())

    await user.keyboard('{Control>}/{/Control}')
    expect(search()).not.toHaveFocus()

    await user.click(screen.getByRole('button', { name: 'Source filter' }))
    await screen.findByRole('menu')
    await user.keyboard('/')
    // the open menu hides the rest of the page
    expect(screen.getByRole('searchbox', { hidden: true })).not.toHaveFocus()
    expect(fireEvent.keyDown(document.activeElement!, { key: '/' })).toBe(true)
  })

  it('the first Tab reaches "Skip to jobs", which moves past the cards and charts', async () => {
    const user = userEvent.setup()
    await loaded()
    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to jobs' })
    expect(skip).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('region', { name: 'Jobs' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('tab', { selected: true })).toHaveFocus()
  })

  it('skipping does not change the address', async () => {
    const { router } = await loaded('/?tab=applied')
    // a followed #jobs link would put the hash in the browser's address (the memory router here ignores it)
    expect(fireEvent.click(screen.getByRole('link', { name: 'Skip to jobs' }))).toBe(false)
    expect(locationOf(router)).toBe('/?tab=applied')
  })

  it('the tabs control the panel with the filters and the table, named by the selected tab', async () => {
    const user = userEvent.setup()
    await loaded('/?tab=scored')
    const panel = screen.getByRole('tabpanel', { name: 'All scored' })
    expect(within(panel).getByRole('table')).toBeInTheDocument()
    expect(within(panel).getByRole('group', { name: 'Filters' })).toBeInTheDocument()
    for (const tab of screen.getAllByRole('tab')) expect(tab).toHaveAttribute('aria-controls', panel.id)
    await user.click(screen.getByRole('tab', { name: 'Filtered out' }))
    expect(await screen.findByRole('tabpanel', { name: 'Filtered out' })).toBe(panel)
  })
})

describe('Overview: footer', () => {
  afterEach(() => vi.unstubAllEnvs())

  it('shows the version and names the docs when no repository is set', async () => {
    vi.stubEnv('VITE_REPO_URL', '')
    await loaded()
    const footer = screen.getByRole('contentinfo')
    expect(footer).toHaveTextContent(`Dashboard v${APP_VERSION}`)
    expect(footer).toHaveTextContent('docs/dashboard.md in the repository')
    expect(within(footer).queryByRole('link')).not.toBeInTheDocument()
  })

  it('links the docs on GitHub when the repository is set', async () => {
    vi.stubEnv('VITE_REPO_URL', 'https://github.com/me/ai-job-search-n8n')
    await loaded()
    const link = within(screen.getByRole('contentinfo')).getByRole('link', { name: 'How it works (docs/dashboard.md)' })
    expect(link).toHaveAttribute('href', 'https://github.com/me/ai-job-search-n8n/blob/main/docs/dashboard.md')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })
})
