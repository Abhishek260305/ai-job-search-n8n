// axe-core over each screen: labels, names, roles, ARIA, landmarks, lists, focusable content. Colour contrast is left
// out because jsdom does not lay out or style; src/lib/contrast.test.ts checks it from the real colour values.
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/render'
import { emailItem, HEALTH, jobDetail, jobPage, jobRow, server, TRACKING, unauthorized } from '@/test/server'

/** `popup`: a menu is open. Radix renders it in a portal outside the landmarks, as popups are; the rest still applies. */
async function violations({ popup = false } = {}): Promise<string[]> {
  const result = await axe.run(document.body, {
    rules: { 'color-contrast': { enabled: false }, ...(popup ? { region: { enabled: false } } : {}) },
    resultTypes: ['violations'],
  })
  return result.violations.map((v) => `${v.id} (${v.help}): ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`)
}

const ROWS = [
  jobRow(1, { company: 'Acme', has_resume: true, has_cover_letter: true, needs_attention: true, closing_soon: true, deadline: '2026-10-20' }),
  jobRow(2, { pipeline_status: 'applied', application_status: 'applied', scam_risk: 'medium', is_remote: true, sources: ['lever', 'ashby', 'greenhouse'] }),
  jobRow(3, { pipeline_status: 'scam_blocked', job_status: 'scam_blocked', score: null }),
]

async function overview(path = '/') {
  server.use(http.get('/api/jobs', () => HttpResponse.json(jobPage(ROWS, 120))))
  renderApp(path)
  // hidden: an open job's panel hides the page behind it
  await screen.findByRole('table', { hidden: true })
  await screen.findByRole('img', { name: /^Applications by status:/, hidden: true }, { timeout: 5000 })
}

describe('accessibility (axe)', () => {
  it('the login page', async () => {
    server.use(http.get('/api/me', unauthorized))
    renderApp('/login')
    await screen.findByLabelText('Password')
    expect(await violations()).toEqual([])
  })

  it('the overview: header, cards, charts, tabs, filters, table, pager, footer', async () => {
    await overview()
    expect(await violations()).toEqual([])
  })

  it('the health page', async () => {
    server.use(http.get('/api/pipeline', () => HttpResponse.json({
      ...HEALTH,
      alerts: [{ level: 'bad', code: 'paused', message: 'Everything is paused.' }, { level: 'warn', code: 'parked', message: '1 job parked.' }],
      paused: { paused: true, paused_at: '2026-10-12T08:00:00Z', reason: 'away' },
      parked: { total: 1, items: [{ stage: 'score', job_id: 9, title: 'Engineer', company: 'Acme', claims: 6, strikes: 0 }] },
      sources: { tracked: 3, failing: 1, items: [{ source: 'lever', key: 'x', last_fetched_at: null, last_ok_at: null, last_count: null, failures: 3, last_error: 'HTTP 404' }] },
    })))
    renderApp('/health')
    await screen.findByRole('list', { name: 'Schedules' })
    expect(await violations()).toEqual([])
  })

  it('the pause dialog', async () => {
    renderApp('/health')
    await userEvent.click(await screen.findByRole('button', { name: 'Pause everything' }))
    await screen.findByRole('dialog')
    expect(await violations({ popup: true })).toEqual([])
  })

  it('the spend page', async () => {
    renderApp('/spend')
    await screen.findByRole('img', { name: /^Spending per day/ })
    await userEvent.click(screen.getByText('Show the numbers'))
    expect(await violations()).toEqual([])
  })

  it('the settings page', async () => {
    renderApp('/settings')
    await screen.findByRole('heading', { name: 'Optional parts' }, { timeout: 5000 })
    expect(await violations()).toEqual([])
  })

  it('the settings page with a refusal and a validation message showing', async () => {
    server.use(http.patch('/api/settings/schedules/score', () => HttpResponse.json({ detail: 'refused' }, { status: 422 })))
    renderApp('/settings')
    const user = userEvent.setup()
    const form = within(await screen.findByRole('form', { name: 'Score jobs schedule' }))
    await user.clear(form.getByLabelText('Jobs per run')); await user.type(form.getByLabelText('Jobs per run'), 'ten')
    expect(await violations()).toEqual([])
    await user.clear(form.getByLabelText('Jobs per run')); await user.type(form.getByLabelText('Jobs per run'), '9')
    await user.click(form.getByRole('button', { name: 'Save' }))
    await form.findByRole('alert')
    expect(await violations()).toEqual([])
  })

  it('the run-now dialog', async () => {
    renderApp('/settings')
    await userEvent.click((await within(await screen.findByRole('form', { name: 'Score jobs schedule' })).findByRole('button', { name: 'Run now' })))
    await screen.findByRole('dialog')
    expect(await violations({ popup: true })).toEqual([])
  })

  it('the search page', async () => {
    renderApp('/search')
    await screen.findByRole('heading', { name: 'Company boards' }, { timeout: 5000 })
    expect(await violations()).toEqual([])
  })

  it('the search page with matches, an error and an unsaved change showing', async () => {
    server.use(
      http.post('/api/search/boards/find', () => HttpResponse.json({ name: 'Spotify', tried: ['spotify'], matches: [{ system: 'lever', slug: 'spotify', jobs: 9 }] })),
      http.put('/api/search', () => HttpResponse.json({ detail: 'locations: at least one location is required; sources: x' }, { status: 422 })),
    )
    renderApp('/search')
    const user = userEvent.setup()
    await user.type(await screen.findByRole('textbox', { name: 'Find a company' }, { timeout: 5000 }), 'Spotify')
    await user.click(screen.getByRole('button', { name: 'Find' }))
    await screen.findByRole('list', { name: 'Matches' })
    await user.type(screen.getByLabelText('Companies to skip'), '{Enter}Globex')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await screen.findByRole('alert')
    expect(await violations()).toEqual([])
  })

  it('the board', async () => {
    renderApp('/board')
    await screen.findByRole('list', { name: 'Board columns' }, { timeout: 5000 })
    await screen.findByLabelText('Application numbers')
    expect(await violations()).toEqual([])
  })

  it('the board with the week table open and a job with its history open', async () => {
    server.use(http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id), { application_status: 'applied', pipeline_status: 'applied' }))))
    renderApp('/board?job=3')
    await screen.findByRole('list', { name: 'Status history' }, { timeout: 5000 })
    expect(await violations()).toEqual([])
  })

  it('the board with follow-ups due and the days setting', async () => {
    renderApp('/board')
    await screen.findByRole('list', { name: 'Due for a follow-up' }, { timeout: 5000 })
    await screen.findByLabelText('Remind me when an application has had no reply for')
    expect(await violations()).toEqual([])
  })

  it('a job panel with the notes form filled in, an invalid email and a conflict showing', async () => {
    server.use(
      http.get('/api/jobs/:id/tracking', () => HttpResponse.json(TRACKING)),
      http.put('/api/jobs/:id/tracking', () => HttpResponse.json({ detail: 'These notes were changed elsewhere (another tab or device). Reload to see the latest version.' }, { status: 409 })),
    )
    renderApp('/board?job=3')
    const form = await screen.findByRole('form', { name: 'Notes and follow-up' }, { timeout: 5000 })
    const user = userEvent.setup()
    await user.type(within(form).getByLabelText('Notes'), ' x')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await within(form).findByRole('alert')
    await user.clear(within(form).getByLabelText('Contact email'))
    await user.type(within(form).getByLabelText('Contact email'), 'nope')
    await within(form).findByText('That does not look like an email address.')
    expect(await violations()).toEqual([])
  })

  it('a job history with the undo confirmation open', async () => {
    server.use(http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id), { application_status: 'applied', pipeline_status: 'applied' }))))
    renderApp('/board?job=3')
    const history = await screen.findByRole('list', { name: 'Status history' }, { timeout: 5000 })
    await userEvent.click(within(history).getByRole('button', { name: 'Undo this change' }))
    await within(history).findByRole('group', { name: 'Confirm the undo' })
    expect(await violations()).toEqual([])
  })

  it('the tailoring confirmation and a finished run', async () => {
    server.use(
      http.get('/api/jobs/:id/tailor/estimate', () => HttpResponse.json({
        job_id: 1, can_run: true, reason: null, mode: 'manual', model: 'claude-sonnet-5-5', cover_letter: true, uses_model: true, variant: 'tailored',
        typical_usd: 0.047, at_most_usd: 0.31, existing: { status: 'drafted', has_resume: true, has_cover_letter: true },
        budget: { day_remaining: 1.4, month_remaining: 9, blocked: false, blocked_reason: null } })),
      http.get('/api/jobs/:id/tailor', () => HttpResponse.json({ state: 'done', started_at: 1, finished_at: 2, result: { status: 'drafted', needs_attention: true,
        attention: ['the resume is 2 pages (limit 1)'], cost_usd: 0.047, requirements: { matched: 4, bridged: 1, gap: 0 } } })),
    )
    renderApp('/?job=1')
    await screen.findByText('Ready for your review. It cost $0.05.', {}, { timeout: 5000 })
    await userEvent.click(await screen.findByRole('button', { name: /Tailor (this job|again)…/ }))
    await screen.findByText(/Costs about/)
    expect(await violations({ popup: true })).toEqual([])
  })

  it('a job open in the panel', async () => {
    server.use(http.get('/api/jobs/:id', () => HttpResponse.json(jobDetail(1, {
      application_status: 'approved', pipeline_status: 'approved', has_resume: true, scam_risk: 'medium',
      scam_flags: [{ code: 'NO_COMPANY', severity: 2, detail: 'no company' }],
      gates: { location: { verdict: 'FAIL', note: 'onsite' } }, review_note: 'ok', cost_usd: 0.02, model: 'm',
      description: Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n'),
    }))))
    await overview('/?job=1')
    await screen.findByRole('dialog', { name: 'Engineer 1' })
    expect(await violations()).toEqual([])
  })

  it('the status menu and the note dialog', async () => {
    await overview()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Status: Not applied. Change status' }))
    await screen.findByRole('menu')
    expect(await violations({ popup: true })).toEqual([])
    await user.click(screen.getByRole('menuitem', { name: 'Add a note…' }))
    await screen.findByRole('dialog', { name: 'Change the status' })
    expect(await violations()).toEqual([])
  })

  it('an open filter menu', async () => {
    await overview()
    await userEvent.click(screen.getByRole('button', { name: 'Source filter' }))
    await screen.findByRole('menu')
    expect(await violations({ popup: true })).toEqual([])
  })

  it('an empty table and an error', async () => {
    server.use(
      http.get('/api/jobs', () => HttpResponse.json(jobPage([], 0))),
      http.get('/api/summary', () => HttpResponse.json({ detail: 'the database is busy' }, { status: 503 })),
    )
    renderApp('/?q=nothing')
    await screen.findByText('No jobs match these filters.')
    await screen.findAllByRole('alert')
    expect(await violations()).toEqual([])
  })

  it('the email page, with a flagged suggestion, a stale one, and the tabs', async () => {
    server.use(
      http.get('/api/email/count', () => HttpResponse.json({ enabled: true, pending: 3 })),
      http.get('/api/email', () => HttpResponse.json({
        enabled: true, login: 'ok', counts: { pending: 3, accepted: 1, dismissed: 0 },
        items: [
          emailItem(1),
          emailItem(2, { signal: 'offer', proposed_status: 'offer', confidence: 'low', flags: ['phishing_risk', 'unverified_sender', 'classified_by_model'], needs_ack: true, source: 'model', company: 'Beta' }),
          emailItem(3, { stale: true, can_accept: false, current_status: 'rejected', company: 'Gamma' }),
        ],
      })),
    )
    renderApp('/email')
    await screen.findByRole('heading', { name: /Beta/ })
    expect(await violations()).toEqual([])
  })

  it('the email page when the part is off, and when the login expired', async () => {
    server.use(http.get('/api/email', () => HttpResponse.json({ enabled: false, login: 'none', counts: { pending: 0, accepted: 0, dismissed: 0 }, items: [] })))
    renderApp('/email')
    await screen.findByRole('note')
    expect(await violations()).toEqual([])
  })
})
