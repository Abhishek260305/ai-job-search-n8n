import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { addDays, forgetDraft } from '@/lib/tracking'
import { renderApp } from '@/test/render'
import { BOARD, jobDetail, NO_TRACKING, server, TRACKING } from '@/test/server'

const TODAY = BOARD.today

// unsaved drafts live as long as the page, so every test starts without one
beforeEach(() => { for (const id of [1, 2, 3, 4]) forgetDraft(id) })

type Put = { id: string; body: Record<string, unknown>; header: string | null }

/** A tracking endpoint that holds one job's notes the way the API does: versions change on each save, an old version is a 409. */
function trackingApi(initial = TRACKING) {
  let current = { ...initial }
  let n = 0
  const puts: Put[] = []
  const gets: string[] = []
  server.use(
    http.get('/api/jobs/:id/tracking', ({ params }) => { gets.push(String(params.id)); return HttpResponse.json(current) }),
    http.put('/api/jobs/:id/tracking', async ({ request, params }) => {
      const body = (await request.json()) as Record<string, unknown>
      puts.push({ id: String(params.id), body, header: request.headers.get('x-requested-with') })
      if (body.version !== current.version) {
        return HttpResponse.json({ detail: 'These notes were changed elsewhere (another tab or device). Reload to see the latest version.' }, { status: 409 })
      }
      const changes = Object.fromEntries(Object.entries(body).filter(([key]) => key !== 'version'))
      current = { ...current, ...changes, version: `v${++n}` } as typeof current
      return HttpResponse.json(current)
    }),
    http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id)))),
  )
  return { puts, gets, elsewhere: (changes: Partial<typeof TRACKING>) => { current = { ...current, ...changes, version: 'other-device' } } }
}

async function openForm(path = '/?job=1') {
  renderApp(path)
  return screen.findByRole('form', { name: 'Notes and follow-up' }, { timeout: 5000 })
}

describe('the notes form in a job panel', () => {
  it('shows what is saved, and Save stays off until something changes', async () => {
    trackingApi()
    const form = await openForm()
    expect(within(form).getByLabelText('Notes')).toHaveValue('call Priya on Friday')
    expect(within(form).getByLabelText('Contact')).toHaveValue('Priya N')
    expect(within(form).getByLabelText('Contact email')).toHaveValue('priya@example.com')
    expect(within(form).getByLabelText('Referred by')).toHaveValue('via Sam')
    expect(within(form).getByLabelText('Follow up on')).toHaveValue('2026-10-16')
    expect(within(form).getByRole('link', { name: 'Write to priya@example.com' })).toHaveAttribute('href', 'mailto:priya@example.com')
    expect(within(form).getByRole('button', { name: 'Save' })).toHaveAttribute('aria-disabled', 'true')
    expect(within(form).getByRole('button', { name: 'Discard changes' })).toHaveAttribute('aria-disabled', 'true')
    expect(within(form).getByLabelText('Notes')).toHaveAttribute('maxlength', '2000')
  })

  it('works for a job with nothing saved yet', async () => {
    server.use(http.get('/api/jobs/:id/tracking', () => HttpResponse.json(NO_TRACKING)))
    const form = await openForm()
    expect(within(form).getByLabelText('Notes')).toHaveValue('')
    expect(within(form).queryByRole('link', { name: /Write to/ })).not.toBeInTheDocument()
  })

  it('saves only what changed with the version it read, and the saved version replaces the draft', async () => {
    const api = trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    const notes = within(form).getByLabelText('Notes')
    await user.clear(notes)
    await user.type(notes, 'moved to Monday')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.puts).toHaveLength(1))
    expect(api.puts[0]).toMatchObject({ id: '1', body: { version: TRACKING.version, notes: 'moved to Monday' }, header: expect.any(String) })
    expect(Object.keys(api.puts[0].body).sort()).toEqual(['notes', 'version'])
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    await waitFor(() => expect(within(screen.getByRole('form', { name: 'Notes and follow-up' })).getByRole('button', { name: 'Save' })).toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByLabelText('Notes')).toHaveValue('moved to Monday')
  })

  it('clears a field by emptying it, and sets the date with the quick buttons', async () => {
    const api = trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    await user.clear(within(form).getByLabelText('Referred by'))
    await user.click(within(form).getByRole('button', { name: 'In 3 days' }))
    expect(within(form).getByLabelText('Follow up on')).toHaveValue(addDays(TODAY, 3))
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.puts).toHaveLength(1))
    expect(api.puts[0].body).toEqual({ version: TRACKING.version, referral: null, follow_up_on: addDays(TODAY, 3) })
    await user.click(await screen.findByRole('button', { name: 'Clear date' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.puts).toHaveLength(2))
    expect(api.puts[1].body).toMatchObject({ follow_up_on: null })
  })

  it('refuses a bad email in the form, before anything is sent', async () => {
    const api = trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    const email = within(form).getByLabelText('Contact email')
    await user.clear(email)
    await user.type(email, 'not-an-email')
    expect(await within(form).findByText('That does not look like an email address.')).toBeInTheDocument()
    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(within(form).getByRole('button', { name: 'Save' })).toHaveAttribute('aria-disabled', 'true')
    fireEvent.submit(form)
    expect(api.puts).toEqual([])
  })

  it('says so when the notes were changed elsewhere, keeps what you typed, and loads the latest on request', async () => {
    const api = trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    api.elsewhere({ notes: 'written on the phone' })
    const notes = within(form).getByLabelText('Notes')
    await user.clear(notes)
    await user.type(notes, 'typed on the laptop')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent('changed elsewhere')
    expect(within(form).getByLabelText('Notes')).toHaveValue('typed on the laptop')
    await user.click(within(alert).getByRole('button', { name: 'Load the latest' }))
    await waitFor(() => expect(screen.getByLabelText('Notes')).toHaveValue('written on the phone'))
    expect(screen.queryByRole('button', { name: 'Load the latest' })).not.toBeInTheDocument()
  })

  it('can save your changes on top of the latest: the fields you changed win, the others keep their latest value', async () => {
    const api = trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    api.elsewhere({ notes: 'written on the phone', contact_name: 'Ravi (from the phone)' })
    const notes = within(form).getByLabelText('Notes')
    await user.clear(notes)
    await user.type(notes, 'typed on the laptop')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await user.click(await within(form).findByRole('button', { name: 'Save my changes on top' }))
    await waitFor(() => expect(api.puts).toHaveLength(2))
    expect(api.puts[1].body).toEqual({ version: 'other-device', notes: 'typed on the laptop' })      // only the field I changed, on the latest version
    await waitFor(() => expect(screen.getByLabelText('Contact')).toHaveValue('Ravi (from the phone)'))
    expect(screen.getByLabelText('Notes')).toHaveValue('typed on the laptop')
  })

  it('keeps what you typed when the panel is closed and opened again', async () => {
    trackingApi()
    const { router } = renderApp('/?job=1')
    const form = await screen.findByRole('form', { name: 'Notes and follow-up' }, { timeout: 5000 })
    const user = userEvent.setup()
    await user.type(within(form).getByLabelText('Notes'), ' and a second thought')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(router.state.location.search).toBe(''))
    await user.click(await screen.findByRole('button', { name: 'Engineer 1' }))
    const again = await screen.findByRole('form', { name: 'Notes and follow-up' })
    await waitFor(() => expect(within(again).getByLabelText('Notes')).toHaveValue('call Priya on Friday and a second thought'))
    expect(within(again).getByText('Your unsaved changes from earlier are back.')).toBeInTheDocument()
    await user.click(within(again).getByRole('button', { name: 'Discard changes' }))
    expect(within(again).getByLabelText('Notes')).toHaveValue('call Priya on Friday')
    expect(within(again).queryByText('Your unsaved changes from earlier are back.')).not.toBeInTheDocument()
  })

  it('keeps the keyboard where it was after a save, and says so for a screen reader', async () => {
    trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    await user.type(within(form).getByLabelText('Notes'), ' x')
    const save = within(form).getByRole('button', { name: 'Save' })
    save.focus()
    await user.keyboard('{Enter}')
    await within(form).findByText('Saved.')
    expect(within(form).getByRole('status')).toHaveTextContent('Saved.')
    expect(document.activeElement).toBe(within(form).getByRole('button', { name: 'Save' }))
  })

  it('counts "in 3 days" and "in a week" from the server day, not the browser one', async () => {
    trackingApi({ ...TRACKING, follow_up_on: null as unknown as string, today: '2031-12-30' })
    const form = await openForm()
    await userEvent.click(within(form).getByRole('button', { name: 'In 3 days' }))
    expect(within(form).getByLabelText('Follow up on')).toHaveValue('2032-01-02')
    await userEvent.click(within(form).getByRole('button', { name: 'In a week' }))
    expect(within(form).getByLabelText('Follow up on')).toHaveValue('2032-01-06')
  })

  it('reloads the board after a save (a follow-up date decides what is due)', async () => {
    trackingApi()
    let reads = 0
    server.use(http.get('/api/board', () => { reads += 1; return HttpResponse.json(BOARD) }))
    renderApp('/board?job=3')
    const form = await screen.findByRole('form', { name: 'Notes and follow-up' }, { timeout: 5000 })
    await waitFor(() => expect(reads).toBe(1))
    await userEvent.type(within(form).getByLabelText('Notes'), ' x')
    await userEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(reads).toBe(2))
  })

  it('does not ask for the notes again when the panel is reopened (the cache is the draft\'s base)', async () => {
    const api = trackingApi()
    renderApp('/?job=1')
    await screen.findByRole('form', { name: 'Notes and follow-up' }, { timeout: 5000 })
    expect(api.gets).toEqual(['1'])
    await userEvent.keyboard('{Escape}')
    await userEvent.click(await screen.findByRole('button', { name: 'Engineer 1' }))
    await screen.findByRole('form', { name: 'Notes and follow-up' })
    expect(api.gets).toEqual(['1'])
  })

  it('clears an old error when the changes are discarded', async () => {
    server.use(
      http.get('/api/jobs/:id/tracking', () => HttpResponse.json(TRACKING)),
      http.put('/api/jobs/:id/tracking', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })),
    )
    const form = await openForm()
    const user = userEvent.setup()
    await user.type(within(form).getByLabelText('Notes'), ' x')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await within(form).findByText('the database is not available right now')
    await user.click(within(form).getByRole('button', { name: 'Discard changes' }))
    expect(within(form).queryByText('the database is not available right now')).not.toBeInTheDocument()
  })

  it('discards changes back to what is saved', async () => {
    trackingApi()
    const form = await openForm()
    const user = userEvent.setup()
    await user.type(within(form).getByLabelText('Notes'), ' more')
    await user.click(within(form).getByRole('button', { name: 'Discard changes' }))
    expect(within(form).getByLabelText('Notes')).toHaveValue('call Priya on Friday')
  })

  it('does not replace what is being typed when the window gets focus back', async () => {
    const api = trackingApi()
    const form = await openForm()
    await userEvent.type(within(form).getByLabelText('Notes'), ' and more')
    const before = api.gets.length
    window.dispatchEvent(new Event('focus'))
    window.dispatchEvent(new Event('visibilitychange'))            // what react-query listens to
    await new Promise((r) => setTimeout(r, 50))
    expect(api.gets).toHaveLength(before)
    expect(within(form).getByLabelText('Notes')).toHaveValue('call Priya on Friday and more')
  })

  it('says when the notes cannot be loaded, and retries', async () => {
    let fail = true
    server.use(http.get('/api/jobs/:id/tracking', () => (fail ? HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 }) : HttpResponse.json(NO_TRACKING))))
    renderApp('/?job=1')
    const alert = await screen.findByText(/The notes could not be loaded/, {}, { timeout: 5000 })
    expect(alert).toHaveTextContent('the database is not available right now')
    fail = false
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('form', { name: 'Notes and follow-up' })).toBeInTheDocument()
  })
})

describe('the follow-up list on the board', () => {
  it('lists what is due with the reason, the contact and the status', async () => {
    renderApp('/board')
    const list = await screen.findByRole('list', { name: 'Due for a follow-up' })
    const [waiting, dated] = within(list).getAllByRole('listitem')
    expect(waiting).toHaveTextContent('Engineer 3')
    expect(waiting).toHaveTextContent('Applied 11 days ago, no reply')
    expect(waiting).toHaveTextContent('Priya N')
    expect(within(waiting).getByRole('link', { name: 'priya@example.com' })).toHaveAttribute('href', 'mailto:priya@example.com')
    expect(dated).toHaveTextContent('Your follow-up date was 2026-10-10')
    expect(dated).toHaveTextContent('referred by Sam')
    expect(screen.getByRole('heading', { name: 'Follow up (2)' })).toBeInTheDocument()
  })

  it('says when nothing is due', async () => {
    server.use(http.get('/api/board', () => HttpResponse.json({ ...BOARD, follow_ups: [], follow_ups_total: 0 })))
    renderApp('/board')
    expect(await screen.findByText(/Nothing is due/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Follow up' })).toBeInTheDocument()
  })

  it('marks a card that is due', async () => {
    const card = { ...BOARD.columns[1].items[0], follow_up: 'waiting' as const }
    server.use(http.get('/api/board', () => HttpResponse.json({ ...BOARD, columns: [BOARD.columns[0], { ...BOARD.columns[1], items: [card] }, ...BOARD.columns.slice(2)] })))
    renderApp('/board')
    const li = (await within(await screen.findByRole('region', { name: 'Applied' })).findByText('Engineer 3')).closest('li') as HTMLElement
    expect(within(li).getByText('Follow up')).toBeInTheDocument()
  })

  it('counts the reminder from the server day, and uses the days you set for "Followed up"', async () => {
    const api = trackingApi()
    server.use(http.get('/api/board', () => HttpResponse.json({ ...BOARD, today: '2026-12-30', follow_up_days: 10 })))
    renderApp('/board')
    const list = await screen.findByRole('list', { name: 'Due for a follow-up' })
    await userEvent.click(within(list).getByRole('button', { name: 'Followed up on Engineer 3: remind me again in 10 days' }))
    await waitFor(() => expect(api.puts).toHaveLength(1))
    expect(api.puts[0].body).toMatchObject({ follow_up_on: '2027-01-09' })
    await userEvent.click(within(list).getByRole('button', { name: 'Snooze 3 days for Engineer 4' }))
    await waitFor(() => expect(api.puts).toHaveLength(2))
    expect(api.puts[1].body).toMatchObject({ follow_up_on: '2027-01-02' })
  })

  it('holds a row while it saves, even after another row was clicked', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    const puts: string[] = []
    server.use(
      http.get('/api/jobs/:id/tracking', () => HttpResponse.json(TRACKING)),
      http.put('/api/jobs/:id/tracking', async ({ params }) => { puts.push(String(params.id)); await gate; return HttpResponse.json(TRACKING) }),
    )
    renderApp('/board')
    const list = await screen.findByRole('list', { name: 'Due for a follow-up' })
    const snooze3 = within(list).getByRole('button', { name: 'Snooze 3 days for Engineer 3' })
    await userEvent.click(snooze3)
    await waitFor(() => expect(snooze3).toBeDisabled())
    await userEvent.click(within(list).getByRole('button', { name: 'Snooze 3 days for Engineer 4' }))
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(snooze3).toBeDisabled()                                       // still saving: one request each, no second click possible
    release()
    await waitFor(() => expect(snooze3).toBeEnabled())
  })

  it('"Followed up" sets the next reminder the set number of days ahead, from the latest version', async () => {
    const api = trackingApi()
    renderApp('/board')
    const list = await screen.findByRole('list', { name: 'Due for a follow-up' })
    await userEvent.click(within(list).getByRole('button', { name: 'Followed up on Engineer 3: remind me again in 7 days' }))
    await waitFor(() => expect(api.puts).toHaveLength(1))
    expect(api.gets).toContain('3')
    expect(api.puts[0]).toMatchObject({ id: '3', body: { version: TRACKING.version, follow_up_on: addDays(TODAY, 7) } })
  })

  it('"Snooze 3 days" sets the date three days ahead and reloads the board', async () => {
    const api = trackingApi()
    let reads = 0
    server.use(http.get('/api/board', () => { reads += 1; return HttpResponse.json(BOARD) }))
    renderApp('/board')
    const list = await screen.findByRole('list', { name: 'Due for a follow-up' })
    await waitFor(() => expect(reads).toBe(1))
    await userEvent.click(within(list).getByRole('button', { name: 'Snooze 3 days for Engineer 4' }))
    await waitFor(() => expect(api.puts).toHaveLength(1))
    expect(api.puts[0].body).toMatchObject({ follow_up_on: addDays(TODAY, 3) })
    await waitFor(() => expect(reads).toBe(2))
  })

  it('shows the refusal when a reminder cannot be changed', async () => {
    server.use(
      http.get('/api/jobs/:id/tracking', () => HttpResponse.json(TRACKING)),
      http.put('/api/jobs/:id/tracking', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })),
    )
    renderApp('/board')
    await userEvent.click(await screen.findByRole('button', { name: 'Snooze 3 days for Engineer 3' }))
    expect(await screen.findByText('the database is not available right now')).toBeInTheDocument()
  })

  it('shows the number of days as the server has it after a reload (a change made on another device)', async () => {
    let current = 7
    server.use(http.get('/api/board', () => HttpResponse.json({ ...BOARD, follow_up_days: current })))
    renderApp('/board')
    expect(await screen.findByLabelText('Remind me when an application has had no reply for')).toHaveValue(7)
    current = 12
    await userEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(screen.getByLabelText('Remind me when an application has had no reply for')).toHaveValue(12))
  })

  it('changes the number of days, and only offers a valid number', async () => {
    const puts: unknown[] = []
    server.use(http.put('/api/follow-ups/days', async ({ request }) => { puts.push(await request.json()); return HttpResponse.json({ days: 10 }) }))
    renderApp('/board')
    const user = userEvent.setup()
    let current = 7
    server.use(http.get('/api/board', () => HttpResponse.json({ ...BOARD, follow_up_days: current })))
    const days = await screen.findByLabelText('Remind me when an application has had no reply for')
    const save = within(screen.getByRole('form', { name: 'Follow-up reminder setting' })).getByRole('button', { name: 'Save' })
    expect(days).toHaveValue(7)
    expect(save).toBeDisabled()                                       // unchanged
    await user.clear(days)
    await user.type(days, '0')
    expect(save).toBeDisabled()
    expect(days).toHaveAttribute('aria-invalid', 'true')
    await user.clear(days)
    await user.type(days, '91')
    expect(save).toBeDisabled()                                       // 90 is the most
    await user.clear(days)
    await user.type(days, '10')
    await user.click(save)
    current = 10
    await waitFor(() => expect(puts).toEqual([{ days: 10 }]))
    await waitFor(() => expect(screen.getByLabelText('Remind me when an application has had no reply for')).toHaveValue(10))   // the board reloaded with the new setting
  })
})
