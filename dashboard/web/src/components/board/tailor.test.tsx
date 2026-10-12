import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { JobDetail, TailorEstimate, TailorState } from '@/api/types'
import { renderApp } from '@/test/render'
import { toast } from 'sonner'
import { vi } from 'vitest'
import { BOARD, EVENTS, jobDetail, server, SPEND, SUMMARY } from '@/test/server'

const ESTIMATE: TailorEstimate = {
  job_id: 1, can_run: true, reason: null, mode: 'manual', model: 'claude-sonnet-5-5', cover_letter: true, uses_model: true, variant: 'tailored',
  typical_usd: 0.047, at_most_usd: 0.31, existing: null,
  budget: { day_remaining: 1.4, month_remaining: 9, blocked: false, blocked_reason: null },
}
const DONE: TailorState = {
  state: 'done', started_at: 1, finished_at: 40,
  result: { job_id: 1, status: 'drafted', variant: 'tailored', needs_attention: true, attention: ['the resume is 2 pages (limit 1)'], cost_usd: 0.047,
            has_cover_letter: true, pages: 2, requirements: { matched: 4, bridged: 1, gap: 0 } },
}

/** The tailoring endpoints as the scraper would answer them, recording what the page asked and sent. */
function backend(over: { job?: Partial<JobDetail>; estimate?: Partial<TailorEstimate>; status?: TailorState[]; startError?: Response } = {}) {
  const seen = { estimates: 0, starts: [] as Record<string, unknown>[], statuses: 0, jobs: 0, boards: 0 }
  const statuses = [...(over.status ?? [{ state: 'idle' } as TailorState])]
  server.use(
    http.get('/api/jobs/:id', ({ params }) => { seen.jobs += 1; return HttpResponse.json(jobDetail(Number(params.id), over.job)) }),
    http.get('/api/jobs/:id/tailor/estimate', () => { seen.estimates += 1; return HttpResponse.json({ ...ESTIMATE, ...over.estimate }) }),
    http.get('/api/jobs/:id/tailor', () => { seen.statuses += 1; return HttpResponse.json(statuses.length > 1 ? statuses.shift() : statuses[0]) }),
    http.post('/api/jobs/:id/tailor', async ({ request }) => {
      seen.starts.push((await request.json()) as Record<string, unknown>)
      return over.startError ?? HttpResponse.json({ state: 'running', started_at: 1 })
    }),
    http.get('/api/board', () => { seen.boards += 1; return HttpResponse.json({ detail: 'not used here' }, { status: 503 }) }),
  )
  return seen
}

const open = async (path = '/?job=1') => { renderApp(path); return screen.findByRole('button', { name: /Tailor (this job|again)…/ }, { timeout: 5000 }) }

describe('the Tailor this job button', () => {
  it('is offered for a scored job, says it works with automatic tailoring off, and asks nothing until it is pressed', async () => {
    const seen = backend()
    const button = await open()
    expect(button).toHaveTextContent('Tailor this job…')
    expect(screen.getByText(/works even when automatic tailoring is off/)).toBeInTheDocument()
    expect(seen.estimates).toBe(0)
  })

  it('says "Tailor again" when there is already a resume', async () => {
    backend({ job: { has_resume: true } })
    expect(await open()).toHaveTextContent('Tailor again…')
  })

  it.each([
    ['a job that was never scored', { score: null }],
    ['a scam-blocked job', { job_status: 'scam_blocked' as const }],
    ['a job already applied to', { job_status: 'applied' as const }],
    ['a filtered-out job', { job_status: 'filtered_out' as const }],
  ])('is not offered for %s', async (_name, job) => {
    backend({ job })
    renderApp('/?job=1')
    await screen.findByRole('dialog', { name: 'Engineer 1' }, { timeout: 5000 })
    await screen.findByText('Found on')
    expect(screen.queryByRole('button', { name: /Tailor (this job|again)…/ })).not.toBeInTheDocument()
  })
})

describe('the confirmation', () => {
  it('shows the cost (typical and at most), what is left in the budget, and that automatic tailoring is off', async () => {
    const seen = backend()
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    expect(await within(dialog).findByText(/Costs about \$0\.05, at most \$0\.31/)).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Left in the budget: $1.40 today, $9.00 this month. Model: claude-sonnet-5-5.')
    expect(dialog).toHaveTextContent('Automatic tailoring is off, so this runs only because you asked.')
    expect(within(dialog).getByRole('button', { name: 'Tailor now (up to $0.31)' })).toBeInTheDocument()
    expect(seen.starts).toEqual([])
  })

  it('warns that it replaces what is there', async () => {
    backend({ job: { has_resume: true }, estimate: { existing: { status: 'drafted', has_resume: true, has_cover_letter: true } } })
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job again?' })
    expect(await within(dialog).findByText(/replaces the resume and cover letter made earlier \(status: drafted\)/)).toBeInTheDocument()
  })

  it('says it costs nothing when no call to Claude is made', async () => {
    backend({ estimate: { uses_model: false, at_most_usd: 0, typical_usd: 0, variant: 'base' } })
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    expect(await within(dialog).findByText('Costs nothing.')).toBeInTheDocument()
    expect(dialog).not.toHaveTextContent('Left in the budget')
  })

  it('cannot start when the scraper says no, and shows why', async () => {
    const seen = backend({ estimate: { can_run: false, reason: 'tailoring is switched off (jobsearch features set tailoring on)' } })
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    expect(await within(dialog).findByText(/tailoring is switched off/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Turn Tailoring on under Settings, Optional parts/)).toBeInTheDocument()
    const go = within(dialog).getByRole('button', { name: /Tailor now/ })
    expect(go).toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(go)
    expect(seen.starts).toEqual([])
  })

  it('does not suggest switching anything on when the reason is a different one', async () => {
    backend({ estimate: { can_run: false, reason: 'this job was tailored a moment ago: wait about 90 seconds before doing it again' } })
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    expect(await within(dialog).findByText(/a moment ago/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/Turn Tailoring on/)).not.toBeInTheDocument()
  })

  it('does not start before the cost is known', async () => {
    const seen = backend()
    server.use(http.get('/api/jobs/:id/tailor/estimate', async () => { await new Promise((r) => setTimeout(r, 300)); return HttpResponse.json(ESTIMATE) }))
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await userEvent.click(within(dialog).getByRole('button', { name: /Tailor now/ }))
    expect(seen.starts).toEqual([])
  })

  it('cancel starts nothing', async () => {
    const seen = backend()
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await within(dialog).findByText(/Costs about/)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Tailor this job?' })).not.toBeInTheDocument())
    expect(seen.starts).toEqual([])
  })

  it('shows the scraper\'s refusal inside the dialog and keeps it open', async () => {
    backend({ startError: HttpResponse.json({ detail: 'another job is being tailored right now: wait for it to finish' }, { status: 422 }) })
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await within(dialog).findByText(/Costs about/)
    await userEvent.click(within(dialog).getByRole('button', { name: /Tailor now/ }))
    expect(await within(dialog).findByText('another job is being tailored right now: wait for it to finish')).toBeInTheDocument()
  })
})

describe('a run', () => {
  it('starts with the cost the user was shown, shows that it is running, and reports the result when it ends', async () => {
    const seen = backend({ status: [{ state: 'idle' }, { state: 'running', started_at: 1 }, { state: 'running', started_at: 1 }, DONE] })
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await userEvent.click(await within(dialog).findByRole('button', { name: 'Tailor now (up to $0.31)' }))
    await waitFor(() => expect(seen.starts).toHaveLength(1))
    expect(seen.starts[0]).toEqual({ max_cost_usd: 0.31 })                    // exactly the "at most" it showed
    expect(await screen.findByText(/Tailoring this job… usually about a minute/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Tailor (this job|again)…/ })).not.toBeInTheDocument()     // nothing to press twice
    const jobsBefore = seen.jobs
    expect(await screen.findByText('Ready for your review. It cost $0.05.', {}, { timeout: 9000 })).toBeInTheDocument()
    expect(screen.getByText("Matches 4 of the posting's 5 requirements, 1 close.")).toBeInTheDocument()
    expect(screen.getByText('the resume is 2 pages (limit 1)')).toBeInTheDocument()
    await waitFor(() => expect(seen.jobs).toBeGreaterThan(jobsBefore))             // the job reloads: its files are there now
    expect(await screen.findByRole('button', { name: /Tailor (this job|again)…/ })).toBeInTheDocument()
  }, 20_000)

  it('shows a run that is already going when the panel is opened', async () => {
    backend({ status: [{ state: 'running', started_at: 1 }] })
    renderApp('/?job=1')
    expect(await screen.findByText(/Tailoring this job… usually about a minute/, {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Tailor (this job|again)…/ })).not.toBeInTheDocument()
  })

  it('shows what an earlier run reported when the panel is opened again', async () => {
    backend({ status: [DONE] })
    renderApp('/?job=1')
    expect(await screen.findByText('Ready for your review. It cost $0.05.', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('says plainly when a run could not afford itself and spent nothing', async () => {
    backend({ status: [{ state: 'done', started_at: 1, finished_at: 2, result: { status: 'budget_paused', reason: 'not enough budget left for this job', attention: [] } }] })
    renderApp('/?job=1')
    expect(await screen.findByText('Not run: not enough budget left for this job. Nothing was spent.', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('shows a run that failed, with the reason', async () => {
    backend({ status: [{ state: 'failed', started_at: 1, finished_at: 2, error: 'the PDF service is down' }] })
    renderApp('/?job=1')
    expect(await screen.findByText(/Tailoring did not finish: the PDF service is down/, {}, { timeout: 5000 })).toBeInTheDocument()
  })
})


describe('what the review of this feature asked for', () => {
  it('sends one request however often "Tailor now" is pressed while the first is being sent', async () => {
    const seen = backend()
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    server.use(http.post('/api/jobs/:id/tailor', async ({ request }) => { seen.starts.push((await request.json()) as Record<string, unknown>); await gate; return HttpResponse.json({ state: 'running', started_at: 1 }) }))
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    const go = await within(dialog).findByRole('button', { name: 'Tailor now (up to $0.31)' })
    await userEvent.click(go)
    await userEvent.click(go)
    await userEvent.click(go)
    await waitFor(() => expect(seen.starts).toHaveLength(1))
    await new Promise((r) => setTimeout(r, 100))
    expect(seen.starts).toHaveLength(1)
    release()
  })

  it('cannot be closed while the request is being sent', async () => {
    const seen = backend()
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    server.use(http.post('/api/jobs/:id/tailor', async ({ request }) => { seen.starts.push((await request.json()) as Record<string, unknown>); await gate; return HttpResponse.json({ state: 'running', started_at: 1 }) }))
    await userEvent.click(await open())
    const dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await userEvent.click(await within(dialog).findByRole('button', { name: 'Tailor now (up to $0.31)' }))
    await waitFor(() => expect(seen.starts).toHaveLength(1))
    await userEvent.keyboard('{Escape}')
    expect(screen.getByRole('dialog', { name: 'Tailor this job?' })).toBeInTheDocument()
    release()
  })

  it('asks for the cost afresh each time, and does not show an old refusal when reopened', async () => {
    const seen = backend({ startError: HttpResponse.json({ detail: 'another job is being tailored right now: wait for it to finish' }, { status: 422 }) })
    await userEvent.click(await open())
    let dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await userEvent.click(await within(dialog).findByRole('button', { name: /Tailor now/ }))
    await within(dialog).findByText('another job is being tailored right now: wait for it to finish')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Tailor this job?' })).not.toBeInTheDocument())
    await userEvent.click(await screen.findByRole('button', { name: /Tailor (this job|again)…/ }))
    dialog = await screen.findByRole('dialog', { name: 'Tailor this job?' })
    await within(dialog).findByText(/Costs about/)
    expect(within(dialog).queryByText(/another job is being tailored/)).not.toBeInTheDocument()
    expect(seen.estimates).toBe(2)
  })

  it('warns what re-tailoring does to a decision already made', async () => {
    backend({ job: { has_resume: true }, estimate: { existing: { status: 'approved', has_resume: true, has_cover_letter: true } } })
    await userEvent.click(await open())
    expect(await screen.findByText(/Your approval and note are cleared/)).toBeInTheDocument()
  })

  it('warns that a skipped job is no longer skipped', async () => {
    backend({ job: { has_resume: true }, estimate: { existing: { status: 'skipped', has_resume: true, has_cover_letter: false } } })
    await userEvent.click(await open())
    expect(await screen.findByText(/It is no longer skipped/)).toBeInTheDocument()
  })

  it('is not offered when the application was submitted even if the job still looks open', async () => {
    backend({ job: { job_status: 'scored', application_status: 'interview', pipeline_status: 'interview' } })
    renderApp('/?job=1')
    await screen.findByText('Found on', {}, { timeout: 5000 })
    expect(screen.queryByRole('button', { name: /Tailor (this job|again)…/ })).not.toBeInTheDocument()
  })

  it('does not announce or reload anything for a run that had already finished when the panel was opened', async () => {
    const spy = vi.spyOn(toast, 'success')
    const seen = backend({ status: [DONE] })
    renderApp('/?job=1')
    await screen.findByText('Ready for your review. It cost $0.05.', {}, { timeout: 5000 })
    const jobs = seen.jobs
    await new Promise((r) => setTimeout(r, 300))
    expect(spy.mock.calls.filter(([m]) => String(m).startsWith('Tailored'))).toEqual([])
    expect(seen.jobs).toBe(jobs)
    spy.mockRestore()
  })

  it('says a run that failed cost money when it did', async () => {
    backend({ status: [{ state: 'failed', started_at: 1, finished_at: 2, error: 'the PDF service is down', cost_usd: 0.0184 }] })
    renderApp('/?job=1')
    expect(await screen.findByText(/Tailoring did not finish: the PDF service is down\. It cost \$0\.02\./, {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('reloads the board, the numbers, the history and the spend when a run finishes, and stops asking', async () => {
    const reads = { board: 0, summary: 0, events: 0, spend: 0 }       // (Spend is not open here: it is only marked stale)
    const seen = backend({ job: { application_status: 'drafted', pipeline_status: 'drafted' }, status: [{ state: 'running', started_at: 1 }, DONE] })      // a drafted job shows its history
    server.use(
      http.get('/api/board', () => { reads.board += 1; return HttpResponse.json(BOARD) }),
      http.get('/api/summary', () => { reads.summary += 1; return HttpResponse.json(SUMMARY) }),
      http.get('/api/jobs/:id/events', () => { reads.events += 1; return HttpResponse.json(EVENTS) }),
      http.get('/api/spend', () => { reads.spend += 1; return HttpResponse.json(SPEND) }),
    )
    renderApp('/?job=1')
    await screen.findByText(/Tailoring this job…/, {}, { timeout: 5000 })
    const before = { ...reads }
    const jobsBefore = seen.jobs
    expect(await screen.findByText('Ready for your review. It cost $0.05.', {}, { timeout: 9000 })).toBeInTheDocument()
    await waitFor(() => expect(reads.summary).toBeGreaterThan(before.summary))
    await waitFor(() => expect(reads.events).toBeGreaterThan(before.events))
    expect(seen.jobs).toBeGreaterThan(jobsBefore)
    const statuses = seen.statuses
    await new Promise((r) => setTimeout(r, 3500))
    expect(seen.statuses).toBe(statuses)
  }, 25_000)

  it('says so when the service forgot the run (it was restarted)', async () => {
    const spy = vi.spyOn(toast, 'warning')
    backend({ status: [{ state: 'running', started_at: 1 }, { state: 'idle' }] })
    renderApp('/?job=1')
    await screen.findByText(/Tailoring this job…/, {}, { timeout: 5000 })
    await waitFor(() => expect(spy.mock.calls.some(([m]) => String(m).includes('interrupted'))).toBe(true), { timeout: 9000 })
    spy.mockRestore()
  }, 25_000)
})
