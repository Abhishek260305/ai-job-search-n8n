import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { SettingsPage } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { server, SETTINGS, unauthorized } from '@/test/server'

const page = (over: Partial<SettingsPage> = {}): SettingsPage => ({ ...SETTINGS, ...over })

/** Records the body and header of every write to `url`, answering with `answer` (default: the page unchanged). */
function writes(method: 'patch' | 'put' | 'post', url: string, answer: () => Response = () => HttpResponse.json(SETTINGS)) {
  const seen: { body: unknown; header: string | null }[] = []
  server.use(http[method](url, async ({ request }) => {
    seen.push({ body: await request.json().catch(() => null), header: request.headers.get('x-requested-with') })
    return answer()
  }))
  return seen
}

const refusal = (detail: string) => () => HttpResponse.json({ detail }, { status: 422 })
const schedule = async (name: string) => within(await screen.findByRole('form', { name: `${name} schedule` }))

describe('the settings page', () => {
  it('is reached from the header and shows every section', async () => {
    const { router } = renderApp('/')
    await userEvent.click(await screen.findByRole('link', { name: 'Settings' }))
    expect(await screen.findByRole('heading', { name: 'Settings' }, { timeout: 5000 })).toBeInTheDocument()
    expect(locationOf(router)).toBe('/settings')
    for (const name of ['Optional parts', 'Schedules', 'Tailoring', 'Spending caps']) {
      expect(await screen.findByRole('heading', { name })).toBeInTheDocument()
    }
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page')
  })

  it('lists the five stages and the four optional parts, the Gmail part among them', async () => {
    renderApp('/settings')
    await schedule('Collect jobs')
    const forms = screen.getAllByRole('form').map((f) => f.getAttribute('aria-label'))
    expect(forms.filter((n) => n?.endsWith(' schedule'))).toEqual(
      ['Collect jobs schedule', 'Score jobs schedule', 'Tailor applications schedule', 'Send the digest schedule', 'Check mail schedule'])
    const parts = within(screen.getByRole('list', { name: 'Optional parts' })).getAllByRole('listitem')
    expect(parts.map((p) => p.querySelector('label')?.textContent)).toEqual(['Tailoring', 'Telegram', 'Digest', 'Email replies'])
    const mail = within(parts[3])
    expect(mail.getByRole('checkbox')).not.toBeChecked()
    expect(mail.getByText(/Not in effect: switched off \(jobsearch features set --gmail on\)/)).toBeInTheDocument()
    expect(mail.getByText(/read-only/)).toBeInTheDocument()
  })

  it('names what the mail stage does per run', async () => {
    renderApp('/settings')
    const mail = await schedule('Check mail')
    expect(mail.getByLabelText('Emails opened per run')).toHaveValue('20')
    expect(mail.getByLabelText('Runs every')).toHaveValue('30')
  })

  it('switches the Gmail part on and off like the other parts, saving at once', async () => {
    const seen: unknown[] = []
    server.use(http.patch('/api/settings/features', async ({ request }) => {
      seen.push(await request.json())
      return HttpResponse.json(SETTINGS)
    }))
    const user = userEvent.setup()
    renderApp('/settings')
    const box = await screen.findByRole('checkbox', { name: 'Email replies' })
    await user.click(box)
    await waitFor(() => expect(seen).toEqual([{ gmail: true }]))
  })

  it('shows the saved values in plain units', async () => {
    renderApp('/settings')
    const collect = await schedule('Collect jobs')
    expect(collect.getByLabelText('Runs every')).toHaveValue('6')
    expect(collect.getByLabelText('Unit')).toHaveValue('hours')
    expect(collect.getByLabelText('Searches per run')).toHaveValue('18')
    expect(collect.getByLabelText('Company boards and feeds per run')).toHaveValue('35')
    expect(collect.getByText('15 minutes to 7 days, kept gentle on the job boards')).toBeInTheDocument()
    const score = await schedule('Score jobs')
    expect(score.getByLabelText('Runs every')).toHaveValue('30')
    expect(score.getByLabelText('Unit')).toHaveValue('minutes')
    expect(score.getByText('claude-haiku-5-5')).toBeInTheDocument()             // shown, not editable
    expect(score.queryByLabelText('Model')).not.toBeInTheDocument()
    expect(screen.getByText(/scripts\/jobsearch schedule set score --model/)).toBeInTheDocument()
    const digest = await schedule('Send the digest')
    expect(digest.getByLabelText('Time of day (HH:MM, 24-hour)')).toHaveValue('08:00')
    expect(screen.getByText(/Daily times are in Asia\/Kolkata/)).toBeInTheDocument()
    expect(screen.getByText('scripts/jobsearch schedule timezone')).toBeInTheDocument()
    expect(screen.queryByLabelText('Time zone name')).not.toBeInTheDocument()       // it moves the day a budget counts from
  })

  it('reports a failed first load with a retry, and an unreachable scraper in its own words', async () => {
    server.use(http.get('/api/settings', () => HttpResponse.json({ detail: 'the scraper is not reachable right now' }, { status: 503 })))
    renderApp('/settings')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The settings could not be loaded.')
    expect(alert).toHaveTextContent('the scraper is not reachable right now')
    server.use(http.get('/api/settings', () => HttpResponse.json(SETTINGS)))
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Optional parts' })).toBeInTheDocument()
  })

  it('goes to the login page when the session has ended', async () => {
    server.use(http.get('/api/settings', unauthorized), http.get('/api/me', unauthorized))
    const { router } = renderApp('/settings')
    await waitFor(() => expect(locationOf(router)).toContain('/login'))
  })
})

describe('schedules', () => {
  it('saves only what changed, with the write header, and shows the page the server sends back', async () => {
    const seen = writes('patch', '/api/settings/schedules/score', () => HttpResponse.json(page({
      schedules: SETTINGS.schedules.map((s) => (s.name === 'score' ? { ...s, every_minutes: 60, batch: 10 } : s)) })))
    renderApp('/settings')
    const user = userEvent.setup()
    const score = await schedule('Score jobs')
    expect(score.getByRole('button', { name: 'Save' })).toBeDisabled()                // nothing changed yet
    await user.clear(score.getByLabelText('Runs every')); await user.type(score.getByLabelText('Runs every'), '1')
    await user.selectOptions(score.getByLabelText('Unit'), 'hours')
    await user.clear(score.getByLabelText('Jobs per run')); await user.type(score.getByLabelText('Jobs per run'), '10')
    await user.click(score.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(seen).toEqual([{ body: { every_minutes: 60, batch: 10 }, header: 'jobsearch-dashboard' }]))
    expect(await screen.findByText('Schedule saved')).toBeInTheDocument()
    await waitFor(() => expect((screen.getByRole('form', { name: 'Score jobs schedule' }).querySelector('#schedule-score-batch') as HTMLInputElement).value).toBe('10'))
  })

  it('turns a stage off', async () => {
    const seen = writes('patch', '/api/settings/schedules/tailor')
    renderApp('/settings')
    const tailor = await schedule('Tailor applications')
    await userEvent.click(tailor.getByRole('checkbox', { name: 'On' }))
    await userEvent.click(tailor.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(seen.map((s) => s.body)).toEqual([{ enabled: false }]))
  })

  it('catches text that is not a number before sending anything', async () => {
    const seen = writes('patch', '/api/settings/schedules/score')
    renderApp('/settings')
    const user = userEvent.setup()
    const score = await schedule('Score jobs')
    await user.clear(score.getByLabelText('Jobs per run')); await user.type(score.getByLabelText('Jobs per run'), 'ten')
    expect(score.getByText('The number per run must be a whole number.')).toBeInTheDocument()
    expect(score.getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.clear(score.getByLabelText('Runs every')); await user.type(score.getByLabelText('Runs every'), 'soon')
    expect(seen).toEqual([])
  })

  it('shows the scraper\'s own refusal next to the form and keeps what was typed', async () => {
    writes('patch', '/api/settings/schedules/collect', refusal('collect can run every 15 minutes to 7 days (kept gentle on the job boards)'))
    renderApp('/settings')
    const user = userEvent.setup()
    const collect = await schedule('Collect jobs')
    await user.selectOptions(collect.getByLabelText('Unit'), 'minutes')
    await user.clear(collect.getByLabelText('Runs every')); await user.type(collect.getByLabelText('Runs every'), '5')
    await user.click(collect.getByRole('button', { name: 'Save' }))
    expect(await collect.findByRole('alert')).toHaveTextContent('collect can run every 15 minutes to 7 days')
    expect(collect.getByLabelText('Runs every')).toHaveValue('5')
  })

  it('sends the digest\'s time of day, or its interval, whichever is chosen', async () => {
    const seen = writes('patch', '/api/settings/schedules/digest')
    renderApp('/settings')
    const user = userEvent.setup()
    const digest = await schedule('Send the digest')
    await user.clear(digest.getByLabelText('Time of day (HH:MM, 24-hour)')); await user.type(digest.getByLabelText('Time of day (HH:MM, 24-hour)'), '20:30')
    await user.click(digest.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(seen.map((s) => s.body)).toEqual([{ at_time: '20:30' }]))
    await user.click(digest.getByRole('radio', { name: 'At an interval' }))
    expect(await digest.findByLabelText('Runs every')).toBeInTheDocument()
    await user.clear(digest.getByLabelText('Runs every')); await user.type(digest.getByLabelText('Runs every'), '6')
    await user.selectOptions(digest.getByLabelText('Unit'), 'hours')
    await user.click(digest.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(seen.map((s) => s.body)).toEqual([{ at_time: '20:30' }, { every_minutes: 360 }]))
  })

  it('asks first before "Run now", then requests the run', async () => {
    const seen = writes('post', '/api/settings/schedules/collect/run', () => HttpResponse.json({ requested: 'collect', note: 'it starts at the next scheduler tick (within 5 minutes)' }))
    renderApp('/settings')
    const user = userEvent.setup()
    const collect = await schedule('Collect jobs')
    await user.click(collect.getByRole('button', { name: 'Run now' }))
    const dialog = await screen.findByRole('dialog', { name: 'Run collect jobs now?' })
    expect(seen).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Run now' }))
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0].header).toBe('jobsearch-dashboard')
    expect(await screen.findByText(/Run requested\. It starts at the next scheduler tick/)).toBeInTheDocument()
  })

  it('cancelling "Run now" requests nothing, and a paused system cannot run', async () => {
    const seen = writes('post', '/api/settings/schedules/score/run')
    renderApp('/settings')
    const user = userEvent.setup()
    await user.click((await schedule('Score jobs')).getByRole('button', { name: 'Run now' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(seen).toEqual([])
  })

  it('disables "Run now" for a stage whose feature is off, and says so', async () => {
    renderApp('/settings')
    const tailor = await schedule('Tailor applications')                            // tailoring is off in the fixture
    expect(tailor.getByRole('button', { name: 'Run now' })).toBeDisabled()
    expect(tailor.getByText('Feature off')).toBeInTheDocument()
    expect((await schedule('Collect jobs')).getByRole('button', { name: 'Run now' })).toBeEnabled()
  })

  it('does not offer to edit the score model, or send one', async () => {
    const seen = writes('patch', '/api/settings/schedules/score')
    renderApp('/settings')
    const score = await schedule('Score jobs')
    expect(score.queryByRole('textbox', { name: /model/i })).not.toBeInTheDocument()
    expect(seen).toEqual([])
  })

  it('disables "Run now" while everything is paused', async () => {
    server.use(http.get('/api/settings', () => HttpResponse.json(page({ paused: true, paused_reason: 'away' }))))
    renderApp('/settings')
    expect((await schedule('Score jobs')).getByRole('button', { name: 'Run now' })).toBeDisabled()
    expect(await screen.findByText(/Everything is paused/)).toBeInTheDocument()
  })

  it('does not wipe a half-typed form when only "last run" changes in the background', async () => {
    renderApp('/settings')
    const user = userEvent.setup()
    const score = await schedule('Score jobs')
    await user.clear(score.getByLabelText('Jobs per run')); await user.type(score.getByLabelText('Jobs per run'), '4')
    server.use(http.get('/api/settings', () => HttpResponse.json(page({
      schedules: SETTINGS.schedules.map((s) => (s.name === 'score' ? { ...s, last_run_at: '2026-10-12T09:40:00Z' } : s)) }))))
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await new Promise((r) => setTimeout(r, 300))
    expect((await schedule('Score jobs')).getByLabelText('Jobs per run')).toHaveValue('4')
  })
})

describe('optional parts', () => {
  it('says what is in effect and why a part is not', async () => {
    renderApp('/settings')
    const list = await screen.findByRole('list', { name: 'Optional parts' })
    const items = within(list).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('Not in effect: switched off (jobsearch features set --tailoring on)')
    expect(items[1]).toHaveTextContent('In effect')
    expect(items[0]).toHaveTextContent('about 5 cents per application')
  })

  it('saves a switch at once', async () => {
    const seen = writes('patch', '/api/settings/features')
    renderApp('/settings')
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Tailoring' }))
    await waitFor(() => expect(seen).toEqual([{ body: { tailoring: true }, header: 'jobsearch-dashboard' }]))
  })

  it('asks before switching Telegram off while approval is "review", and sends nothing until confirmed', async () => {
    const seen = writes('patch', '/api/settings/features')
    renderApp('/settings')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('checkbox', { name: 'Telegram' }))
    const dialog = await screen.findByRole('dialog', { name: 'Switch Telegram off?' })
    expect(dialog).toHaveTextContent('applications are stored as ready with no review step')
    expect(dialog).toHaveTextContent('scripts/jobsearch tailor-settings set --approval cli')
    expect(seen).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Keep Telegram on' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(seen).toEqual([])
    await user.click(screen.getByRole('checkbox', { name: 'Telegram' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Switch it off' }))
    await waitFor(() => expect(seen.map((s) => s.body)).toEqual([{ telegram: false }]))
  })

  it('switches Telegram off without asking when the approval mode is "cli"', async () => {
    server.use(http.get('/api/settings', () => HttpResponse.json(page({ features: { ...SETTINGS.features, approval: 'cli', approval_effective: 'cli' } }))))
    const seen = writes('patch', '/api/settings/features')
    renderApp('/settings')
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Telegram' }))
    await waitFor(() => expect(seen.map((s) => s.body)).toEqual([{ telegram: false }]))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('warns when "review" already behaves as "auto" because Telegram is not in effect', async () => {
    server.use(http.get('/api/settings', () => HttpResponse.json(page({ features: {
      ...SETTINGS.features, approval_effective: 'auto',
      effective: { ...SETTINGS.features.effective, telegram: { on: false, reason: 'not configured: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env (optional)' } } } }))))
    renderApp('/settings')
    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('Approval is review but behaves as auto while Telegram is not in effect')
    expect(note).toHaveTextContent('Choose cli to keep a human decision')
  })

  it('shows no such warning when approval behaves as set', async () => {
    renderApp('/settings')
    await screen.findByRole('list', { name: 'Optional parts' })
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('shows a refusal', async () => {
    writes('patch', '/api/settings/features', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 }))
    renderApp('/settings')
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Digest' }))
    expect(await screen.findByText('the database is not available right now')).toBeInTheDocument()
  })
})

describe('tailoring', () => {
  it('sends changes, and clears an emptied optional number', async () => {
    const seen = writes('patch', '/api/settings/tailoring')
    renderApp('/settings')
    const user = userEvent.setup()
    const form = within(await screen.findByRole('form', { name: 'Tailoring settings' }))
    await user.selectOptions(form.getByLabelText('Mode'), 'manual')
    await user.clear(form.getByLabelText('Score above which your base resume is used as written'))
    await user.clear(form.getByLabelText('Most applications per day')); await user.type(form.getByLabelText('Most applications per day'), '2')
    await user.selectOptions(form.getByLabelText('Paper'), 'letter')
    await user.click(form.getByRole('checkbox', { name: 'Write a cover letter' }))
    await user.click(form.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(seen.map((s) => s.body)).toEqual([{ mode: 'manual', daily_limit: 2, cover_letter: false, paper: 'letter', clear: ['max_score'] }]))
  })

  it('shows the approval mode but offers no way to change it', async () => {
    renderApp('/settings')
    const form = await screen.findByRole('form', { name: 'Tailoring settings' })
    expect(screen.getByText('review', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getByText(/scripts\/jobsearch tailor-settings set --approval/)).toBeInTheDocument()
    expect(within(form).queryByLabelText(/approval/i)).not.toBeInTheDocument()
    expect(within(form).queryByLabelText('Model')).not.toBeInTheDocument()
  })

  it('names the search threshold as the default lowest score', async () => {
    renderApp('/settings')
    expect(await screen.findByText('Empty: your search threshold (60)')).toBeInTheDocument()
  })
})

describe('spending caps: only lower', () => {
  const caps = async () => within(await screen.findByRole('form', { name: 'Spending caps' }))

  it('lowers caps', async () => {
    const seen = writes('patch', '/api/settings/budget')
    renderApp('/settings')
    const user = userEvent.setup()
    const form = await caps()
    await user.clear(form.getByLabelText('Claude, per month (US dollars)')); await user.type(form.getByLabelText('Claude, per month (US dollars)'), '7.5')
    await user.click(form.getByRole('button', { name: 'Lower the caps' }))
    await waitFor(() => expect(seen).toEqual([{ body: { monthly_cap_usd: 7.5 }, header: 'jobsearch-dashboard' }]))
  })

  it('refuses a raise on the page, sends nothing, and says how to do it properly', async () => {
    const seen = writes('patch', '/api/settings/budget')
    renderApp('/settings')
    const user = userEvent.setup()
    const form = await caps()
    await user.clear(form.getByLabelText('Claude, per day (US dollars)')); await user.type(form.getByLabelText('Claude, per day (US dollars)'), '5')
    expect(form.getByText(/can only be lowered here.*scripts\/jobsearch budget set/)).toBeInTheDocument()
    expect(form.getByRole('button', { name: 'Lower the caps' })).toBeDisabled()
    expect(seen).toEqual([])
  })

  it('shows the server\'s refusal if a cap was raised elsewhere in the meantime', async () => {
    writes('patch', '/api/settings/budget', refusal('a cap can only be lowered here (not monthly cap): to raise one, run scripts/jobsearch budget set'))
    renderApp('/settings')
    const user = userEvent.setup()
    const form = await caps()
    await user.clear(form.getByLabelText('Claude, per month (US dollars)')); await user.type(form.getByLabelText('Claude, per month (US dollars)'), '9')
    await user.click(form.getByRole('button', { name: 'Lower the caps' }))
    expect(await form.findByRole('alert')).toHaveTextContent('a cap can only be lowered here')
  })

  it('shows each cap as it is now', async () => {
    renderApp('/settings')
    const form = await caps()
    expect(form.getByLabelText('Apify, per day (US dollars)')).toHaveValue('0.5')
    expect(form.getByText('Now $0.50. Lower only.')).toBeInTheDocument()
  })
})
