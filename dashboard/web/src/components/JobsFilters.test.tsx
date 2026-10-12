import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { locationOf, renderApp } from '@/test/render'
import { jobPage, jobRow, server } from '@/test/server'

function captureJobs() {
  const urls: URL[] = []
  server.use(http.get('/api/jobs', ({ request }) => {
    urls.push(new URL(request.url))
    return HttpResponse.json(jobPage([jobRow(1)], 120))
  }))
  return urls
}

const search = () => screen.getByRole('searchbox', { name: 'Search title or company' })

describe('JobsTabs', () => {
  it('switches the tab, dropping the status filter and the page', async () => {
    const { router } = renderApp('/?status=shortlisted&page=2&remote=true')
    await screen.findByRole('table')
    await userEvent.click(screen.getByRole('tab', { name: /^Applied/ }))
    expect(locationOf(router)).toBe('/?tab=applied&remote=true')
    expect(screen.getByRole('tab', { name: /^Applied/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows counts only where the summary has the exact number', async () => {
    renderApp('/')
    expect(await screen.findByRole('tab', { name: 'Applied (5)' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'All (1,234)' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Shortlisted' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Filtered out' })).toBeInTheDocument()
  })

  it('follows a card click', async () => {
    renderApp('/')
    await userEvent.click(await screen.findByRole('button', { name: /^Jobs found/ }))
    expect(screen.getByRole('tab', { name: 'All (1,234)' })).toHaveAttribute('aria-selected', 'true')
  })
})

describe('JobsFilters', () => {
  it('searches after a pause, with one request for the whole word, from page 1', async () => {
    const urls = captureJobs()
    const { router } = renderApp('/?page=3')
    await screen.findByRole('table')
    await userEvent.type(search(), 'acme')
    expect(locationOf(router)).toBe('/?page=3')
    await waitFor(() => expect(locationOf(router)).toBe('/?q=acme'))
    await waitFor(() => expect(urls.at(-1)!.searchParams.get('q')).toBe('acme'))
    expect(urls.filter((u) => u.searchParams.has('q')).map((u) => u.searchParams.get('q'))).toEqual(['acme'])
  })

  it('clears the search when the box is emptied', async () => {
    const { router } = renderApp('/?q=acme')
    await screen.findByRole('table')
    expect(search()).toHaveValue('acme')
    await userEvent.clear(search())
    await waitFor(() => expect(locationOf(router)).toBe('/'))
  })

  it('empties the box when the URL drops the search (a card click)', async () => {
    renderApp('/?q=acme')
    await screen.findByRole('table')
    await userEvent.click(screen.getByRole('button', { name: /^Interview/ }))
    expect(search()).toHaveValue('')
  })

  it('filters by status from the tab\'s own list', async () => {
    const { router } = renderApp('/?tab=applied')
    await screen.findByRole('table')
    await userEvent.click(screen.getByRole('button', { name: 'Status filter' }))
    const items = await screen.findAllByRole('menuitemcheckbox')
    expect(items.map((i) => i.textContent)).toEqual(['Applied', 'In progress', 'Interview', 'Offer', 'Rejected', 'Withdrawn', 'Ready to apply', 'Skipped'])
    await userEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Interview' }))
    expect(locationOf(router)).toBe('/?tab=applied&status=interview')
    await userEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Offer' }))
    expect(locationOf(router)).toBe('/?tab=applied&status=interview&status=offer')
    expect(screen.getByRole('menuitemcheckbox', { name: 'Interview' })).toHaveAttribute('aria-checked', 'true')
  })

  it('has no status filter on the Filtered out tab', async () => {
    renderApp('/?tab=filtered')
    await screen.findByRole('table')
    expect(screen.queryByRole('button', { name: 'Status filter' })).not.toBeInTheDocument()
  })

  it('filters by source and says how many are picked', async () => {
    const { router } = renderApp('/?page=2')
    await screen.findByRole('table')
    await userEvent.click(screen.getByRole('button', { name: 'Source filter' }))
    await userEvent.click(await screen.findByRole('menuitemcheckbox', { name: 'Lever' }))
    expect(locationOf(router)).toBe('/?source=lever')
    await userEvent.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: 'Source filter: 1 selected' })).toBeInTheDocument()
  })

  it('filters by minimum score, posting age and remote, each from page 1', async () => {
    const urls = captureJobs()
    const { router } = renderApp('/?page=2')
    await screen.findByRole('table')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Minimum score' }), '70')
    expect(locationOf(router)).toBe('/?min_score=70')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Posted within' }), '7')
    expect(locationOf(router)).toBe('/?min_score=70&posted_days=7')
    await userEvent.click(screen.getByRole('checkbox', { name: 'Remote only' }))
    expect(locationOf(router)).toBe('/?min_score=70&remote=true&posted_days=7')
    await waitFor(() => expect(urls.at(-1)!.searchParams.get('remote')).toBe('true'))
    expect(urls.at(-1)!.searchParams.get('page')).toBe('1')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Minimum score' }), '')
    expect(locationOf(router)).toBe('/?remote=true&posted_days=7')
  })

  it('clears every filter but keeps the tab and sort', async () => {
    const { router } = renderApp('/?tab=applied&q=acme&status=offer&source=lever&min_score=70&remote=true&posted_days=7&sort=company&order=asc&page=2')
    await screen.findByRole('table')
    await userEvent.click(within(screen.getByRole('group', { name: 'Filters' })).getByRole('button', { name: 'Clear filters' }))
    expect(locationOf(router)).toBe('/?tab=applied&sort=company&order=asc')
    expect(search()).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument()
  })
})
