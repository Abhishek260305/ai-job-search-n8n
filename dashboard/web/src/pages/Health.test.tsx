import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Health } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { HEALTH, server, unauthorized } from '@/test/server'

const health = (over: Partial<Health> = {}) => server.use(http.get('/api/pipeline', () => HttpResponse.json({ ...HEALTH, ...over })))

describe('the health page', () => {
  it('is reached from the header and shows all clear when nothing is wrong', async () => {
    const { router } = renderApp('/')
    await userEvent.click(await screen.findByRole('link', { name: 'Health' }))
    expect(await screen.findByRole('heading', { name: 'Health' }, { timeout: 5000 })).toBeInTheDocument()
    expect(locationOf(router)).toBe('/health')
    expect(await screen.findByText('All clear: nothing needs your attention.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Health' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Jobs' })).not.toHaveAttribute('aria-current')
  })

  it('lists every stage with how often it runs, and when it last and next runs', async () => {
    renderApp('/health')
    const list = await screen.findByRole('list', { name: 'Schedules' })
    const cards = within(list).getAllByRole('listitem')
    expect(cards.map((c) => within(c).getByRole('heading').textContent)).toEqual(
      ['Collect jobs', 'Score jobs', 'Tailor applications', 'Send the digest', 'Check mail'])
    expect(cards[0]).toHaveTextContent('every 6 h · 18 per run')
    expect(cards[1]).toHaveTextContent('every 30 min · 25 per run · claude-haiku-5-5')
    expect(cards[3]).toHaveTextContent('daily at 08:00')
    expect(cards[4]).toHaveTextContent('every 30 min · 20 per run')
    expect(within(cards[2]).getByText('Off')).toBeInTheDocument()
    expect(within(cards[2]).getByText('never')).toBeInTheDocument()       // an off stage that never ran
    expect(within(cards[0]).getByText('Last run').nextElementSibling?.querySelector('time')).toHaveAttribute('dateTime', '2026-10-12T06:00:00Z')
  })

  it('shows alerts with their level in words, worst first as the server sorts them', async () => {
    health({ alerts: [
      { level: 'bad', code: 'claude_blocked', message: 'Claude spending is stopped: daily cap reached.' },
      { level: 'warn', code: 'parked', message: '2 job(s) ran out of attempts and are skipped until requeued.' },
    ] })
    renderApp('/health')
    const alerts = within(await screen.findByRole('list', { name: 'Alerts' })).getAllByRole('listitem')
    expect(alerts[0]).toHaveTextContent('Needs action: Claude spending is stopped: daily cap reached.')
    expect(alerts[1]).toHaveTextContent('Heads up: 2 job(s) ran out of attempts')
    expect(screen.queryByText('All clear: nothing needs your attention.')).not.toBeInTheDocument()
  })

  it('shows two alerts with the same code, one per overdue stage, without React key warnings', async () => {
    const errors: unknown[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args) => { errors.push(args) })
    health({ alerts: [
      { level: 'warn', code: 'schedule_overdue', message: 'The collect schedule is overdue.' },
      { level: 'warn', code: 'schedule_overdue', message: 'The score schedule is overdue.' },
    ] })
    renderApp('/health')
    const alerts = within(await screen.findByRole('list', { name: 'Alerts' })).getAllByRole('listitem')
    expect(alerts).toHaveLength(2)
    expect(errors).toEqual([])
    spy.mockRestore()
  })

  it('says "Feature off" for a stage the scheduler skips, and "due" for a run that waits for the next tick', async () => {
    health({ schedules: [
      { ...HEALTH.schedules[2], enabled: true, feature_off: true, last_run_at: '2026-10-11T05:10:00Z', next_run_at: '2026-10-11T05:25:00Z' },
      { ...HEALTH.schedules[1], next_run_at: new Date(Date.now() - 2 * 60_000).toISOString() },
    ] })
    renderApp('/health')
    const [tailor, score] = within(await screen.findByRole('list', { name: 'Schedules' })).getAllByRole('listitem')
    expect(within(tailor).getByText('Feature off')).toBeInTheDocument()
    expect(within(tailor).queryByText('Off')).not.toBeInTheDocument()
    expect(within(tailor).getByText('Next run').nextElementSibling).toHaveTextContent('—')
    expect(within(score).getByText('Next run').nextElementSibling).toHaveTextContent('due')
  })

  it('flags an overdue stage', async () => {
    health({ schedules: [{ ...HEALTH.schedules[0], overdue: true }] })
    renderApp('/health')
    expect(await screen.findByText('Overdue')).toBeInTheDocument()
  })

  it('shows the queue, the parked jobs with the command to put them back, and failing boards', async () => {
    health({
      queue: { waiting_to_score: 12, waiting_over_3h: 4, last_scored_at: null },
      parked: { total: 22, items: [{ stage: 'score', job_id: 9, title: 'Backend Engineer', company: 'Acme', claims: 6, strikes: 1 }] },
      sources: { tracked: 31, failing: 1, items: [
        { source: 'lever', key: 'newco', last_fetched_at: '2026-10-12T09:00:00Z', last_ok_at: null, last_count: null, failures: 3, last_error: 'HTTP 404' }] },
    })
    renderApp('/health')
    expect(await screen.findByText('12')).toBeInTheDocument()
    expect(screen.getByText('(4 for over 3 hours)')).toBeInTheDocument()
    const parked = screen.getByRole('list', { name: 'Parked jobs' })
    expect(parked).toHaveTextContent('Backend Engineer @ Acme')
    expect(parked).toHaveTextContent('attempts 6, bad replies 1')
    expect(screen.getByText('…and 21 more.')).toBeInTheDocument()
    expect(screen.getByText('scripts/jobsearch requeue score')).toBeInTheDocument()
    const failing = screen.getByRole('list', { name: 'Failing boards and feeds' })
    expect(failing).toHaveTextContent('Lever / newco')
    expect(failing).toHaveTextContent('failed 3 times in a row')
    expect(failing).toHaveTextContent('HTTP 404')
    expect(failing).toHaveTextContent('Never worked yet')
    expect(screen.getByText('31 tracked, 1 failing.')).toBeInTheDocument()
  })

  it('says so when no board or feed has been fetched yet', async () => {
    health({ sources: { tracked: 0, failing: 0, items: [] } })
    renderApp('/health')
    expect(await screen.findByText(/None fetched yet/)).toBeInTheDocument()
  })

  it('reports a failed first load with a retry, and goes to the login page on 401', async () => {
    let calls = 0
    server.use(http.get('/api/pipeline', () => { calls++; return HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 }) }))
    renderApp('/health')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The health report could not be loaded.')
    expect(alert).toHaveTextContent('the database is not available right now')
    server.use(http.get('/api/pipeline', () => HttpResponse.json(HEALTH)))
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('All clear: nothing needs your attention.')).toBeInTheDocument()
    expect(calls).toBe(1)
  })

  it('goes to the login page when the session has ended', async () => {
    server.use(http.get('/api/pipeline', unauthorized), http.get('/api/me', unauthorized))
    const { router } = renderApp('/health')
    await waitFor(() => expect(locationOf(router)).toContain('/login'))
  })

  it('refreshes only the health report with Refresh, and has no CSV export', async () => {
    let calls = 0, summaryCalls = 0
    server.use(http.get('/api/pipeline', () => { calls++; return HttpResponse.json(HEALTH) }),
      http.get('/api/summary', () => { summaryCalls++; return HttpResponse.json({}) }))
    renderApp('/health')
    await screen.findByRole('list', { name: 'Schedules' })
    expect(screen.queryByRole('link', { name: 'Export CSV' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await expect.poll(() => calls).toBe(2)
    expect(summaryCalls).toBe(0)
  })
})

describe('pause and resume', () => {
  it('asks first, sends the reason, and shows the paused state once saved', async () => {
    const sent: unknown[] = []
    let state = HEALTH.paused
    server.use(
      http.get('/api/pipeline', () => HttpResponse.json({ ...HEALTH, paused: state, alerts: state.paused ? [{ level: 'bad', code: 'paused', message: 'Everything is paused: away. Nothing scheduled runs until you resume.' }] : [] })),
      http.post('/api/pipeline/pause', async ({ request }) => {
        sent.push([request.headers.get('x-requested-with'), await request.json()])
        state = { paused: true, paused_at: new Date().toISOString(), reason: 'away' }
        return HttpResponse.json(state)
      }),
    )
    renderApp('/health')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Pause everything' }))
    const dialog = await screen.findByRole('dialog', { name: 'Pause everything?' })
    expect(sent).toEqual([])                                  // opening the dialog changes nothing
    await user.type(within(dialog).getByLabelText('Reason (optional)'), '  away ')
    await user.click(within(dialog).getByRole('button', { name: 'Pause everything' }))
    await waitFor(() => expect(sent).toEqual([['jobsearch-dashboard', { reason: 'away' }]]))
    expect(await screen.findByRole('heading', { name: 'Paused' })).toBeInTheDocument()
    expect(screen.getByText(/Reason: away/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument()
    expect(screen.getAllByText('paused').length).toBeGreaterThan(0)     // the stages say "paused" instead of a next run
  })

  it('cancel changes nothing', async () => {
    let posts = 0
    server.use(http.post('/api/pipeline/pause', () => { posts++; return HttpResponse.json(HEALTH.paused) }))
    renderApp('/health')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Pause everything' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(posts).toBe(0)
  })

  it('resumes with one click', async () => {
    let state = { paused: true, paused_at: '2026-10-12T08:00:00Z', reason: null as string | null }
    const bodies: unknown[] = []
    server.use(
      http.get('/api/pipeline', () => HttpResponse.json({ ...HEALTH, paused: state })),
      http.post('/api/pipeline/resume', async ({ request }) => {
        bodies.push(await request.json())
        state = { paused: false, paused_at: null as unknown as string, reason: null }
        return HttpResponse.json(state)
      }),
    )
    renderApp('/health')
    await userEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    expect(await screen.findByRole('heading', { name: 'Running' })).toBeInTheDocument()
    expect(bodies).toEqual([{}])
  })

  it('reports a refused change and stays as it was', async () => {
    server.use(http.post('/api/pipeline/pause', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })))
    renderApp('/health')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Pause everything' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Pause everything' }))
    expect(await screen.findByText('the database is not available right now')).toBeInTheDocument()
    // the dialog stays open so the pause can be tried again; behind it the page still says Running
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Running', hidden: true })).toBeInTheDocument()
  })
})
