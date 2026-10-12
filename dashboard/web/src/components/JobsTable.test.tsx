import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { JobPage } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { jobPage, jobRow, server } from '@/test/server'

/** Answers /api/jobs and records every request's query string. */
function captureJobs(respond: (url: URL) => JobPage = () => jobPage([jobRow(1), jobRow(2)])) {
  const urls: URL[] = []
  server.use(http.get('/api/jobs', ({ request }) => {
    const url = new URL(request.url)
    urls.push(url)
    return HttpResponse.json(respond(url))
  }))
  return urls
}

const table = () => screen.findByRole('table')
// within the table: an open job's panel shows the same company and title
const rowOf = (text: string) => within(document.querySelector('table')!).getByText(text).closest('tr')!

describe('JobsTable: rows', () => {
  it('shows every column of a row', async () => {
    captureJobs(() => jobPage([jobRow(1, {
      company: 'Acme', title: 'Senior Data Engineer', location: 'Bengaluru', is_remote: true, salary_min: 1_200_000,
      salary_max: 1_800_000, salary_currency: 'INR', score: 72, pipeline_status: 'approved',
      sources: ['greenhouse', 'lever', 'ashby'], closing_soon: true, deadline: '2026-10-20', scam_risk: 'medium',
      has_resume: true, has_cover_letter: true, needs_attention: true,
    })]))
    renderApp('/')
    await table()
    const row = within(rowOf('Acme'))
    for (const text of ['Senior Data Engineer', 'Bengaluru', 'Remote', '₹12L – ₹18L', '72', 'Ready to apply', 'Greenhouse', 'Lever', '+1', 'Closing soon', 'Medium risk']) {
      expect(row.getByText(text)).toBeInTheDocument()
    }
    expect(row.getByText('+1')).toHaveAttribute('title', 'Ashby')
    expect(row.getByRole('link', { name: 'Apply on boards.greenhouse.io' })).toHaveAttribute('href', 'https://boards.greenhouse.io/c1/jobs/1')
    expect(row.getByRole('link', { name: 'Resume (PDF)' })).toHaveAttribute('href', '/api/jobs/1/files/resume')
    expect(row.getByRole('link', { name: 'Cover letter (PDF)' })).toHaveAttribute('href', '/api/jobs/1/files/cover_letter')
    for (const link of row.getAllByRole('link')) {
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    }
    expect(row.getByRole('img', { name: 'Needs attention' })).toBeInTheDocument()
  })

  it('says how long ago a job was posted, falling back to first seen, never in the future', async () => {
    const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
    captureJobs(() => jobPage([
      jobRow(1, { posted_at: ago(72) }),
      jobRow(2, { posted_at: null, first_seen: ago(5) }),
      jobRow(3, { posted_at: ago(-6) }),
    ]))
    renderApp('/')
    await table()
    expect(within(rowOf('Company 1')).getByText('3d ago')).toHaveAttribute('title', expect.stringMatching(/^Posted /))
    expect(within(rowOf('Company 2')).getByText('5h ago')).toHaveAttribute('title', expect.stringMatching(/^First seen /))
    expect(within(rowOf('Company 3')).getByText('just now')).toBeInTheDocument()
  })

  it('never links an unsafe address and shows "—" for missing values', async () => {
    captureJobs(() => jobPage([jobRow(1, { apply_url: 'javascript:alert(1)', company: null, location: null, score: null })]))
    renderApp('/')
    await table()
    const row = within(screen.getAllByRole('row')[1])
    expect(row.queryByRole('link')).not.toBeInTheDocument()
    expect(row.getAllByText('—').length).toBeGreaterThanOrEqual(4)
  })

  it('renders posting text as text', async () => {
    captureJobs(() => jobPage([jobRow(1, { title: '<img src=x onerror=alert(1)>', company: '<b>Evil</b>' })]))
    const { container } = renderApp('/')
    await table()
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(screen.getByText('<b>Evil</b>')).toBeInTheDocument()
    expect(container.querySelector('tbody img, tbody b')).toBeNull()
  })

  it('colours the score against the threshold and shows the dimension scores', async () => {
    captureJobs(() => jobPage([jobRow(1, { score: 70 }), jobRow(2, { score: 55, scores: null, verdict: null }), jobRow(3, { score: 30 })]))
    renderApp('/')
    await table()
    expect(within(rowOf('Company 1')).getByText('70')).toHaveClass('bg-emerald-100')
    expect(within(rowOf('Company 2')).getByText('55')).toHaveClass('bg-amber-100')
    expect(within(rowOf('Company 3')).getByText('30')).toHaveClass('bg-muted')
    expect(within(rowOf('Company 2')).queryByRole('button', { name: /^Score/ })).not.toBeInTheDocument()
    within(rowOf('Company 1')).getByRole('button', { name: 'Score 70: details' }).focus()
    const tip = await screen.findByRole('tooltip')
    expect(tip).toHaveTextContent('good fit')
    expect(tip).toHaveTextContent('technical72')
    expect(tip).toHaveTextContent('seniority65')
  })

  it('shows skeletons while loading, and an error with a working retry', async () => {
    let calls = 0
    server.use(http.get('/api/jobs', async () => {
      calls++
      if (calls === 1) { await delay(50); return HttpResponse.json({ detail: 'the database is busy' }, { status: 503 }) }
      return HttpResponse.json(jobPage([jobRow(1)]))
    }))
    renderApp('/')
    expect(await screen.findByRole('status', { name: 'Loading jobs' })).toBeInTheDocument()
    const alert = await screen.findByText('The jobs could not be loaded.')
    expect(alert.parentElement).toHaveTextContent('the database is busy')
    await userEvent.click(within(alert.parentElement!).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Company 1')).toBeInTheDocument()
  })
})

describe('JobsTable: the request follows the URL', () => {
  it('sends the view in the URL', async () => {
    const urls = captureJobs()
    renderApp('/?tab=applied&status=interview&source=lever&page=2&page_size=25&sort=company&order=asc')
    await table()
    const q = urls.at(-1)!.searchParams
    expect(q.get('tab')).toBe('applied')
    expect(q.getAll('status')).toEqual(['interview'])
    expect(q.getAll('source')).toEqual(['lever'])
    expect([q.get('page'), q.get('page_size'), q.get('sort'), q.get('order')]).toEqual(['2', '25', 'company', 'asc'])
  })

  it('sorts by a column on click, and flips it on the next click', async () => {
    const urls = captureJobs()
    const { router } = renderApp('/')
    await table()
    expect(screen.getByRole('columnheader', { name: /Score/ })).toHaveAttribute('aria-sort', 'descending')
    await userEvent.click(screen.getByRole('button', { name: 'Company' }))
    expect(locationOf(router)).toBe('/?sort=company&order=asc')
    await waitFor(() => expect(urls.at(-1)!.searchParams.get('sort')).toBe('company'))
    expect(urls.at(-1)!.searchParams.get('order')).toBe('asc')
    expect(screen.getByRole('columnheader', { name: /Company/ })).toHaveAttribute('aria-sort', 'ascending')
    expect(screen.getByRole('columnheader', { name: /Score/ })).toHaveAttribute('aria-sort', 'none')
    await userEvent.click(screen.getByRole('button', { name: 'Company' }))
    // descending is the default order, so the URL leaves it out
    expect(locationOf(router)).toBe('/?sort=company')
    await userEvent.click(screen.getByRole('button', { name: 'Posted' }))
    expect(locationOf(router)).toBe('/?sort=posted')
  })

  it('goes back to page 1 when sorting', async () => {
    captureJobs(() => jobPage([jobRow(1)], 200, 3))
    const { router } = renderApp('/?page=3')
    await table()
    await userEvent.click(screen.getByRole('button', { name: 'Title' }))
    expect(locationOf(router)).toBe('/?sort=title&order=asc')
  })

  it('has no sort button on columns the API cannot sort by', async () => {
    renderApp('/')
    await table()
    for (const name of ['Location', 'Salary', 'Sources', 'Links']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
      expect(screen.getByRole('columnheader', { name })).not.toHaveAttribute('aria-sort')
    }
  })
})

describe('JobsTable: columns per tab', () => {
  it('shows the Reason column only on the Filtered out tab', async () => {
    captureJobs(() => jobPage([jobRow(1, { filter_reason: 'title: intern', job_status: 'filtered_out', pipeline_status: 'filtered_out' })]))
    const { router } = renderApp('/')
    await table()
    expect(screen.queryByRole('columnheader', { name: 'Reason' })).not.toBeInTheDocument()
    expect(screen.queryByText('title: intern')).not.toBeInTheDocument()
    await router.navigate('/?tab=filtered')
    expect(await screen.findByRole('columnheader', { name: 'Reason' })).toBeInTheDocument()
    expect(screen.getByText('title: intern')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Risk' })).not.toBeInTheDocument()
  })

  it('shows the Risk column only on the Scam-blocked tab', async () => {
    captureJobs(() => jobPage([jobRow(1, { scam_risk: 'high' })]))
    renderApp('/?tab=scam')
    expect(await screen.findByRole('columnheader', { name: 'Risk' })).toBeInTheDocument()
    expect(screen.getByText('high')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Reason' })).not.toBeInTheDocument()
  })
})

describe('JobsTable: opening a job', () => {
  it('opens a job from a click anywhere on its row', async () => {
    const { router } = renderApp('/?tab=applied&page=2')
    await table()
    await userEvent.click(within(rowOf('Company 2')).getByText('Pune'))
    expect(locationOf(router)).toBe('/?tab=applied&page=2&job=2')
    expect(rowOf('Company 2')).toHaveAttribute('data-state', 'selected')
    expect(rowOf('Company 1')).not.toHaveAttribute('data-state')
  })

  it('opens a job from its title with the keyboard, keeping the view', async () => {
    const { router } = renderApp('/?tab=applied&remote=true&page=2')
    await table()
    within(rowOf('Company 1')).getByRole('button', { name: 'Engineer 1' }).focus()
    await userEvent.keyboard('{Enter}')
    expect(locationOf(router)).toBe('/?tab=applied&remote=true&page=2&job=1')
  })

  it('does not open the job when a link in the row is clicked', async () => {
    captureJobs(() => jobPage([jobRow(1, { has_resume: true })]))
    const { router } = renderApp('/')
    await table()
    await userEvent.click(screen.getByRole('link', { name: /^Apply on/ }))
    await userEvent.click(screen.getByRole('link', { name: 'Resume (PDF)' }))
    expect(locationOf(router)).toBe('/')
  })
})

describe('JobsTable: empty states', () => {
  it.each([
    ['/', 'No shortlisted jobs right now. New matches arrive after each collect.'],
    ['/?tab=applied', 'Nothing applied yet. Mark a job as applied from the Shortlisted tab.'],
    ['/?tab=scam', 'No scam-blocked jobs.'],
  ])('%s says %s', async (path, message) => {
    captureJobs(() => jobPage([], 0))
    renderApp(path)
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('offers to clear the filters when they hide everything', async () => {
    captureJobs((url) => (url.searchParams.get('remote') === 'true' ? jobPage([], 0) : jobPage([jobRow(1)])))
    const { router } = renderApp('/?tab=applied&remote=true&sort=company')
    expect(await screen.findByText('No jobs match these filters.')).toBeInTheDocument()
    const buttons = screen.getAllByRole('button', { name: 'Clear filters' })
    await userEvent.click(buttons.at(-1)!)
    expect(locationOf(router)).toBe('/?tab=applied&sort=company')
    expect(await screen.findByText('Company 1')).toBeInTheDocument()
  })

  it('offers the first page when the page is past the end', async () => {
    captureJobs((url) => (url.searchParams.get('page') === '9' ? jobPage([], 20, 9) : jobPage([jobRow(1)], 20)))
    const { router } = renderApp('/?page=9')
    expect(await screen.findByText(/This page is past the end/)).toBeInTheDocument()
    expect(screen.getByText('0 of 20')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Go to the first page' }))
    expect(locationOf(router)).toBe('/')
  })
})

describe('JobsTable: pager', () => {
  const many = (page: number, shown: number) => jobPage(Array.from({ length: shown }, (_, i) => jobRow(page * 100 + i)), 263, page)

  it('shows the range and moves between pages', async () => {
    captureJobs((url) => many(Number(url.searchParams.get('page')), 50))
    const { router } = renderApp('/')
    expect(await screen.findByText('1–50 of 263')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(locationOf(router)).toBe('/?page=2')
    expect(await screen.findByText('51–100 of 263')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Previous' }))
    expect(locationOf(router)).toBe('/')
  })

  it('stops at the last page', async () => {
    captureJobs(() => many(6, 13))
    renderApp('/?page=6')
    expect(await screen.findByText('251–263 of 263')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
  })

  it('changes the page size and starts again at page 1', async () => {
    captureJobs((url) => many(Number(url.searchParams.get('page')), Number(url.searchParams.get('page_size'))))
    const { router } = renderApp('/?page=2')
    await screen.findByText('51–100 of 263')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Rows per page' }), '25')
    expect(locationOf(router)).toBe('/?page_size=25')
    expect(await screen.findByText('1–25 of 263')).toBeInTheDocument()
  })
})
