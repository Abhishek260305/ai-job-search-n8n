import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { JobDetail } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { jobDetail, jobPage, jobRow, server } from '@/test/server'

/** Answers /api/jobs/:id with `detail(id)` and counts the requests. */
function details(detail: (id: number) => Partial<JobDetail> = () => ({})) {
  const calls: number[] = []
  server.use(http.get('/api/jobs/:id', ({ params }) => {
    const id = Number(params.id)
    calls.push(id)
    return HttpResponse.json(jobDetail(id, detail(id)))
  }))
  return calls
}

const panel = (name: string | RegExp = /.+/) => screen.findByRole('dialog', { name })

describe('JobDrawer: opening and closing', () => {
  it('opens beside the table when a row is clicked, with the job header', async () => {
    details(() => ({ company: 'Acme', location: 'Bengaluru', is_remote: true, salary_min: 1_200_000, salary_max: 1_800_000, salary_currency: 'INR' }))
    const { router } = renderApp('/?tab=applied')
    await userEvent.click(await screen.findByRole('button', { name: 'Engineer 2' }))
    expect(locationOf(router)).toBe('/?tab=applied&job=2')
    const dialog = within(await panel('Engineer 2'))
    for (const text of ['Acme', 'Bengaluru', 'Remote', '₹12L – ₹18L']) expect(dialog.getByText(text)).toBeInTheDocument()
    expect(dialog.getByRole('button', { name: 'Status: Not applied. Change status' })).toBeInTheDocument()
  })

  it('Escape closes it and clears ?job, keeping the view, and focus goes back to the row', async () => {
    details()
    const { router } = renderApp('/?tab=applied&page=2&job=1')
    await panel('Engineer 1')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(locationOf(router)).toBe('/?tab=applied&page=2')
    expect(document.activeElement).toHaveAttribute('data-job-title', '1')
  })

  it('the close button closes it too', async () => {
    details()
    const { router } = renderApp('/?job=2')
    await userEvent.click(within(await panel('Engineer 2')).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(locationOf(router)).toBe('/')
  })

  it('moves through the jobs on the current page', async () => {
    server.use(http.get('/api/jobs', () => HttpResponse.json(jobPage([jobRow(4), jobRow(8), jobRow(15)], 120))))
    details()
    const { router } = renderApp('/?tab=all&job=8')
    const nav = () => within(screen.getByRole('navigation', { name: 'Jobs on this page' }))
    await panel('Engineer 8')
    expect(nav().getByText('2 of 3 on this page')).toBeInTheDocument()
    await userEvent.click(nav().getByRole('button', { name: 'Next job' }))
    await panel('Engineer 15')
    expect(locationOf(router)).toBe('/?tab=all&job=15')
    expect(nav().getByRole('button', { name: 'Next job' })).toBeDisabled()
    await userEvent.click(nav().getByRole('button', { name: 'Previous job' }))
    await userEvent.click(nav().getByRole('button', { name: 'Previous job' }))
    await panel('Engineer 4')
    expect(locationOf(router)).toBe('/?tab=all&job=4')
    expect(nav().getByRole('button', { name: 'Previous job' })).toBeDisabled()
  })

  it('a job that is not on the page (a shared link) opens without the arrows', async () => {
    details()
    renderApp('/?job=99')
    await panel('Engineer 99')
    const nav = within(screen.getByRole('navigation', { name: 'Jobs on this page' }))
    expect(nav.getByRole('button', { name: 'Previous job' })).toBeDisabled()
    expect(nav.getByRole('button', { name: 'Next job' })).toBeDisabled()
    expect(nav.queryByText(/on this page/)).not.toBeInTheDocument()
  })

  it('shows a placeholder while loading', async () => {
    server.use(http.get('/api/jobs/:id', async () => { await delay('infinite'); return HttpResponse.json({}) }))
    renderApp('/?job=1')
    expect(within(await panel('Loading the job')).getByRole('status', { name: 'Loading the job' })).toBeInTheDocument()
  })

  it('says when the job is gone', async () => {
    server.use(http.get('/api/jobs/:id', () => HttpResponse.json({ detail: 'no job 7' }, { status: 404 })))
    renderApp('/?job=7')
    const dialog = within(await panel('Job not found'))
    expect(dialog.getByText('This job is not in the database any more.')).toBeInTheDocument()
    expect(dialog.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('shows other errors with a retry', async () => {
    let calls = 0
    server.use(http.get('/api/jobs/:id', () => {
      calls++
      return calls === 1
        ? HttpResponse.json({ detail: 'the database is busy' }, { status: 503 })
        : HttpResponse.json(jobDetail(1))
    }))
    renderApp('/?job=1')
    const dialog = within(await panel('The job could not be loaded'))
    expect(dialog.getByText('the database is busy')).toBeInTheDocument()
    await userEvent.click(dialog.getByRole('button', { name: 'Retry' }))
    expect(await panel('Engineer 1')).toBeInTheDocument()
  })
})

describe('JobDrawer: untrusted posting text', () => {
  it('shows markup in a posting as text, never as elements', async () => {
    details(() => ({
      title: '<img src=x onerror=alert(1)>',
      company: '<b>Evil</b>',
      description: 'Apply now <script>alert(1)</script>\n<img src=x onerror=alert(2)>',
      score_reason: '<a href="javascript:alert(3)">reason</a>',
      strengths: ['<i>strong</i>'],
      gates: { eligibility: { verdict: 'FAIL', note: '<u>note</u>' } },
      scam_risk: 'medium',
      scam_flags: [{ code: 'HIDDEN_CONTENT', severity: 2, detail: '<iframe src=x>' }],
    }))
    renderApp('/?job=1')
    const dialog = await panel('<img src=x onerror=alert(1)>')
    for (const text of ['<b>Evil</b>', '<a href="javascript:alert(3)">reason</a>', '<i>strong</i>', '<u>note</u>', '<iframe src=x>']) {
      expect(within(dialog).getByText(text)).toBeInTheDocument()
    }
    expect(within(dialog).getByText(/Apply now <script>alert\(1\)<\/script>/)).toBeInTheDocument()
    expect(dialog.querySelector('script, img, b, i, u, iframe, a[href^="javascript"]')).toBeNull()
  })

  it('links only http(s) addresses', async () => {
    details(() => ({
      apply_url: 'javascript:alert(1)', canonical_url: 'data:text/html,<script>alert(1)</script>',
      sources_detail: [{ source: 'lever', url: 'javascript:alert(2)', apply_url: 'vbscript:x', first_seen: '2026-10-10T10:00:00Z', last_seen: null }],
    }))
    renderApp('/?job=1')
    const dialog = within(await panel('Engineer 1'))
    expect(dialog.queryByRole('link')).not.toBeInTheDocument()
    expect(dialog.getByText('Lever')).toBeInTheDocument()
  })
})

describe('JobDrawer: sections', () => {
  it('links the apply page, the original posting when it differs, and the PDFs', async () => {
    details(() => ({
      apply_url: 'https://jobs.lever.co/acme/1/apply', canonical_url: 'https://acme.com/careers/1',
      has_resume: true, has_cover_letter: true,
    }))
    renderApp('/?job=1')
    const dialog = within(await panel('Engineer 1'))
    expect(dialog.getByRole('link', { name: 'Apply on jobs.lever.co' })).toHaveAttribute('href', 'https://jobs.lever.co/acme/1/apply')
    expect(dialog.getByRole('link', { name: 'Original posting (acme.com)' })).toHaveAttribute('href', 'https://acme.com/careers/1')
    expect(dialog.getByRole('link', { name: 'Resume (PDF)' })).toHaveAttribute('href', '/api/jobs/1/files/resume')
    expect(dialog.getByRole('link', { name: 'Cover letter (PDF)' })).toHaveAttribute('href', '/api/jobs/1/files/cover_letter')
    for (const link of dialog.getAllByRole('link')) {
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    }
  })

  it('does not repeat the apply link as the original posting', async () => {
    details(() => ({ apply_url: 'https://acme.com/careers/1', canonical_url: 'https://acme.com/careers/1' }))
    renderApp('/?job=1')
    const dialog = within(await panel('Engineer 1'))
    expect(dialog.getByRole('link', { name: 'Apply on acme.com' })).toBeInTheDocument()
    expect(dialog.queryByRole('link', { name: /^Original posting/ })).not.toBeInTheDocument()
  })

  it('explains the score: dimensions, reason, strengths, gaps, gates and years asked', async () => {
    details(() => ({
      score: 72, verdict: 'good fit', scores: { technical_fit: 80, seniority: 55, note: 'n/a' },
      score_reason: 'Strong Python and SQL match.', strengths: ['Python', 'SQL'], gaps: ['No Kafka'],
      gates: { eligibility: { verdict: 'PASS', note: '' }, location: { verdict: 'FAIL', note: 'onsite in Berlin' } },
      yoe_min: 3, yoe_max: 5,
    }))
    renderApp('/?job=1')
    const section = within(within(await panel('Engineer 1')).getByRole('region', { name: 'Why this score' }))
    expect(section.getByText('72')).toBeInTheDocument()
    expect(section.getByText('good fit')).toBeInTheDocument()
    expect(section.getByText('threshold 60')).toBeInTheDocument()
    const dims = within(section.getByRole('list', { name: 'Dimension scores' })).getAllByRole('listitem')
    expect(dims.map((d) => d.textContent)).toEqual(['technical fit80', 'seniority55'])
    expect(section.getByText('Strong Python and SQL match.')).toBeInTheDocument()
    for (const text of ['Python', 'SQL', 'No Kafka']) expect(section.getByText(text)).toBeInTheDocument()
    const failed = section.getByText('onsite in Berlin').closest('li')!
    expect(failed).toHaveTextContent('locationFailonsite in Berlin')
    expect(failed).toHaveClass('bg-red-50')
    expect(section.getByText('eligibility').closest('li')).not.toHaveClass('bg-red-50')
    expect(section.getByText('3–5 years')).toBeInTheDocument()
  })

  it('says when a job is not scored yet, and why it was filtered out', async () => {
    details(() => ({ score: null, verdict: null, scores: null, filter_reason: 'title: not an engineering role' }))
    renderApp('/?job=1')
    const section = within(within(await panel('Engineer 1')).getByRole('region', { name: 'Why this score' }))
    expect(section.getByText('Not scored yet.')).toBeInTheDocument()
    expect(section.getByText('title: not an engineering role')).toBeInTheDocument()
  })

  it('shows the safety check only when the risk is not low', async () => {
    details((id) => id === 1
      ? { scam_risk: 'low' }
      : { scam_risk: 'medium', scam_score: 40, scam_flags: [
        { code: 'UNVERIFIED_HOST', severity: 1, detail: 'apply link: unknown host' },
        { code: 'NO_COMPANY', severity: 2, detail: 'posting does not name a company' },
      ] })
    const { router } = renderApp('/?job=1')
    expect(within(await panel('Engineer 1')).queryByRole('region', { name: 'Safety' })).not.toBeInTheDocument()
    await router.navigate('/?job=2')
    const safety = within(within(await panel('Engineer 2')).getByRole('region', { name: 'Safety' }))
    expect(safety.getByText('Medium risk')).toBeInTheDocument()
    expect(safety.getByText(/score 40/)).toBeInTheDocument()
    expect(safety.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'No company: posting does not name a company', 'Unverified host: apply link: unknown host',
    ])
  })

  it('collapses a long description after 40 lines, with Show all', async () => {
    const lines = Array.from({ length: 60 }, (_, i) => `Line ${i + 1}`)
    details(() => ({ description: lines.join('\n') }))
    renderApp('/?job=1')
    const section = within(within(await panel('Engineer 1')).getByRole('region', { name: 'Description' }))
    expect(section.getByText(/Line 40…$/)).toBeInTheDocument()
    expect(section.queryByText(/Line 41/)).not.toBeInTheDocument()
    const toggle = section.getByRole('button', { name: 'Show all' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(toggle)
    expect(section.getByText(/Line 60$/)).toBeInTheDocument()
    expect(section.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('shows a short description whole, and says when there is none', async () => {
    details((id) => ({ description: id === 1 ? 'Short and sweet.' : null }))
    const { router } = renderApp('/?job=1')
    const section = within(within(await panel('Engineer 1')).getByRole('region', { name: 'Description' }))
    expect(section.getByText('Short and sweet.')).toBeInTheDocument()
    expect(section.queryByRole('button')).not.toBeInTheDocument()
    await router.navigate('/?job=2')
    expect(within(await panel('Engineer 2')).getByText('No description was collected.')).toBeInTheDocument()
  })

  it('lists every board the job was found on', async () => {
    details(() => ({
      sources_detail: [
        { source: 'greenhouse', url: 'https://boards.greenhouse.io/acme/jobs/1', apply_url: null, first_seen: '2026-10-01T10:00:00Z', last_seen: '2026-10-12T04:20:00Z' },
        { source: 'linkedin', url: null, apply_url: 'https://www.linkedin.com/jobs/view/9', first_seen: '2026-10-03T10:00:00Z', last_seen: null },
      ],
    }))
    renderApp('/?job=1')
    const items = within(within(await panel('Engineer 1')).getByRole('region', { name: 'Found on' })).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText('Greenhouse')).toBeInTheDocument()
    expect(within(items[0]).getByRole('link', { name: 'boards.greenhouse.io' })).toHaveAttribute('href', 'https://boards.greenhouse.io/acme/jobs/1')
    expect(items[0]).toHaveTextContent(/first seen .*, last seen/)
    expect(within(items[1]).getByRole('link', { name: 'linkedin.com' })).toHaveAttribute('href', 'https://www.linkedin.com/jobs/view/9')
    expect(items[1]).not.toHaveTextContent('last seen')
  })

  it('never says a listing was seen in the future', async () => {
    const ahead = new Date(Date.now() + 6 * 3_600_000).toISOString()
    details(() => ({
      sources_detail: [{ source: 'lever', url: 'https://jobs.lever.co/a/1', apply_url: null, first_seen: '2026-10-01T10:00:00Z', last_seen: ahead }],
    }))
    renderApp('/?job=1')
    const listing = within(within(await panel('Engineer 1')).getByRole('region', { name: 'Found on' })).getByRole('listitem')
    expect(listing).toHaveTextContent('last seen just now')
  })

  it('leaves a missing company or location out of the header', async () => {
    details((id) => (id === 1 ? { company: 'Acme', location: null } : { company: null, location: 'Pune' }))
    const { router } = renderApp('/?job=1')
    const header = (await panel('Engineer 1')).querySelector('[data-slot="sheet-description"]')!
    expect(header.textContent).toBe('Acme')
    await router.navigate('/?job=2')
    expect((await panel('Engineer 2')).querySelector('[data-slot="sheet-description"]')!.textContent).toBe('Pune')
  })

  it('shows the application when there is one', async () => {
    details((id) => id === 1 ? {} : {
      application_status: 'interview', pipeline_status: 'interview', applied_at: '2026-10-05T09:00:00Z',
      review_note: 'looks good', notes: 'recruiter call on Monday', attention_reason: 'page limit exceeded',
      variant: 'tailored', cost_usd: 0.0342, model: 'claude-haiku',
    })
    const { router } = renderApp('/?job=1')
    expect(within(await panel('Engineer 1')).queryByRole('region', { name: 'Application' })).not.toBeInTheDocument()
    await router.navigate('/?job=2')
    const app = within(within(await panel('Engineer 2')).getByRole('region', { name: 'Application' }))
    expect(app.getByText('Interview')).toBeInTheDocument()
    for (const text of ['looks good', 'recruiter call on Monday', 'page limit exceeded', 'Tailored', '$0.034']) {
      expect(app.getByText(text)).toBeInTheDocument()
    }
    expect(app.getByText(/claude-haiku/)).toBeInTheDocument()
  })

  it('changes the status from the panel and shows the reloaded job', async () => {
    let status: JobDetail['pipeline_status'] = 'shortlisted'
    server.use(
      http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id), { pipeline_status: status }))),
      http.patch('/api/jobs/1/status', async ({ request }) => {
        const body = await request.json() as { status: JobDetail['pipeline_status'] }
        status = body.status
        return HttpResponse.json({ id: 1, status })
      }),
    )
    renderApp('/?job=1')
    const dialog = within(await panel('Engineer 1'))
    await userEvent.click(dialog.getByRole('button', { name: 'Status: Not applied. Change status' }))
    await userEvent.click(await screen.findByRole('menuitemradio', { name: 'Applied' }))
    expect(await screen.findByText('Marked Applied')).toBeInTheDocument()
    expect(await dialog.findByRole('button', { name: 'Status: Applied. Change status' })).toBeEnabled()
    expect(screen.getByRole('dialog', { name: 'Engineer 1' })).toBeInTheDocument()
  })
})
