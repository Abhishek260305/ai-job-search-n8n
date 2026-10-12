import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { JobPage } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { jobPage, jobRow, server } from '@/test/server'

/** A phone: the media query for narrow screens matches. */
function onAPhone() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('max-width'), media: query,
    addEventListener: () => {}, removeEventListener: () => {},
  }))
}

function captureJobs(respond: () => JobPage) {
  const urls: URL[] = []
  server.use(http.get('/api/jobs', ({ request }) => {
    urls.push(new URL(request.url))
    return HttpResponse.json(respond())
  }))
  return urls
}

afterEach(() => vi.unstubAllGlobals())

describe('JobsTable on a phone: cards', () => {
  it('shows each job as a card and no table', async () => {
    onAPhone()
    captureJobs(() => jobPage([jobRow(1, {
      company: 'Acme', title: 'Senior Data Engineer', location: 'Bengaluru', is_remote: true, salary_min: 1_200_000,
      salary_max: 1_800_000, salary_currency: 'INR', score: 72, closing_soon: true, scam_risk: 'medium',
      has_resume: true, apply_url: 'https://boards.greenhouse.io/c1/jobs/1',
    }), jobRow(2)]))
    renderApp('/')
    const list = await screen.findByRole('list', { name: 'Jobs' })
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    const cards = within(list).getAllByRole('listitem')
    expect(cards).toHaveLength(2)
    const card = within(cards[0])
    for (const text of ['Senior Data Engineer', 'Acme', 'Bengaluru', 'Remote', '₹12L – ₹18L', '72', 'Closing soon', 'Medium risk']) {
      expect(card.getByText(text)).toBeInTheDocument()
    }
    expect(card.getByRole('link', { name: 'Apply on boards.greenhouse.io' })).toHaveAttribute('rel', 'noopener noreferrer')
    expect(card.getByRole('link', { name: 'Resume (PDF)' })).toHaveAttribute('href', '/api/jobs/1/files/resume')
  })

  it('opens the details when a card or its title is tapped, but not when its status menu is', async () => {
    onAPhone()
    captureJobs(() => jobPage([jobRow(1, { title: 'Platform Engineer' })]))
    const { router } = renderApp('/')
    const user = userEvent.setup()
    const card = within(await screen.findByRole('list', { name: 'Jobs' })).getByRole('listitem')
    await user.click(within(card).getByRole('button', { name: /^Status:/ }))
    await screen.findByRole('menuitemradio', { name: 'Skipped' })
    expect(locationOf(router)).toBe('/')
    await user.keyboard('{Escape}')
    await user.click(within(card).getByRole('button', { name: 'Platform Engineer' }))
    await waitFor(() => expect(locationOf(router)).toContain('job=1'))
  })

  it('sorts from a list and a direction button, because a phone has no column headers', async () => {
    onAPhone()
    const urls = captureJobs(() => jobPage([jobRow(1), jobRow(2)]))
    const { router } = renderApp('/')
    const user = userEvent.setup()
    await screen.findByRole('list', { name: 'Jobs' })
    await user.selectOptions(screen.getByLabelText('Sort by'), 'company')
    await waitFor(() => expect(locationOf(router)).toContain('sort=company'))
    expect(locationOf(router)).toContain('order=asc')           // text sorts start A to Z
    await user.click(screen.getByRole('button', { name: /^Order: ascending/ }))
    await waitFor(() => expect(locationOf(router)).not.toContain('order='))   // descending is the default
    expect(urls.at(-1)!.searchParams.get('sort')).toBe('company')
  })

  it('keeps the table on a wide screen', async () => {
    captureJobs(() => jobPage([jobRow(1)]))
    renderApp('/')
    expect(await screen.findByRole('table')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Jobs' })).not.toBeInTheDocument()
  })
})
