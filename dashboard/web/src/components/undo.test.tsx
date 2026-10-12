import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { toast } from 'sonner'
import { describe, expect, it, vi } from 'vitest'
import { UNDO_WINDOW_MS } from '@/api/hooks'
import type { JobRow } from '@/api/types'
import { renderApp } from '@/test/render'
import { BOARD, boardCard, EVENTS, jobDetail, jobPage, jobRow, server, SUMMARY } from '@/test/server'

type Undo = { id: string; body: Record<string, unknown>; header: string | null }

/** A job that moves with PATCH and moves back with POST .../undo, the way the API does. */
function backend(initial: Partial<JobRow>, undoReply: () => Response | null = () => null, eventId: number | null = 7) {
  let current = jobRow(1, initial)
  const before = current.pipeline_status
  const undos: Undo[] = []
  const reads = { jobs: 0, events: 0, board: 0 }
  server.use(
    http.get('/api/jobs', () => { reads.jobs += 1; return HttpResponse.json(jobPage([current])) }),
    http.get('/api/jobs/:id/events', () => { reads.events += 1; return HttpResponse.json(EVENTS) }),
    http.get('/api/board', () => { reads.board += 1; return HttpResponse.json(BOARD) }),
    http.patch('/api/jobs/1/status', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      current = { ...current, pipeline_status: body.status as JobRow['pipeline_status'] }
      return HttpResponse.json({ id: 1, status: body.status, event_id: eventId })
    }),
    http.post('/api/jobs/:id/undo', async ({ request, params }) => {
      undos.push({ id: String(params.id), body: (await request.json()) as Record<string, unknown>, header: request.headers.get('x-requested-with') })
      const answer = undoReply()
      if (answer) return answer
      current = { ...current, pipeline_status: before }
      return HttpResponse.json({ id: 1, status: before })
    }),
  )
  return { undos, reads, status: () => current.pipeline_status }
}

describe('Undo after a move', () => {
  it('offers Undo on the "Marked" message, and takes the move back', async () => {
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    expect(await screen.findByText('Marked Offer')).toBeInTheDocument()
    const jobsReads = api.reads.jobs
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(api.undos).toHaveLength(1))
    expect(api.undos[0]).toEqual({ id: '1', body: { expected_status: 'offer', event_id: 7 }, header: 'jobsearch-dashboard' })      // tied to the entry the move wrote
    expect(await screen.findByText('Undone: back to Applied')).toBeInTheDocument()
    await waitFor(() => expect(api.reads.jobs).toBeGreaterThan(jobsReads))
    expect(await screen.findByRole('button', { name: 'Status: Applied. Change status' })).toBeInTheDocument()
    expect(api.status()).toBe('applied')
  })

  it('says why, and reloads, when it is too late to undo', async () => {
    const api = backend({ pipeline_status: 'applied' }, () =>
      HttpResponse.json({ detail: 'The history of this job changed since you looked (or there is nothing left to undo). Reload to see it.' }, { status: 409 }))
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    await screen.findByText('Marked Offer')
    const reads = api.reads.jobs
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    expect(await screen.findByText(/history of this job changed/)).toBeInTheDocument()
    await waitFor(() => expect(api.reads.jobs).toBeGreaterThan(reads))
    expect(api.status()).toBe('offer')
  })

  it('shows another refusal as it is, and keeps the job as it is', async () => {
    const api = backend({ pipeline_status: 'applied' }, () => HttpResponse.json({ detail: 'this application has files and cannot be removed' }, { status: 422 }))
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    await screen.findByText('Marked Offer')
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    expect(await screen.findByText('this application has files and cannot be removed')).toBeInTheDocument()
    expect(api.status()).toBe('offer')
  })

  it('also offers Undo after a drop on the board', async () => {
    const api = backend({ pipeline_status: 'shortlisted' })
    server.use(http.get('/api/board', () => HttpResponse.json({
      generated_at: '2026-10-12T09:30:00Z', today: '2026-10-12', follow_up_days: 7, follow_ups_total: 0, follow_ups: [], closed: { withdrawn: 0, skipped: 0 },
      columns: [
        { key: 'not_applied', total: 1, items: [boardCard(1)] }, { key: 'applied', total: 0, items: [] }, { key: 'in_progress', total: 0, items: [] },
        { key: 'interview', total: 0, items: [] }, { key: 'offer', total: 0, items: [] }, { key: 'rejected', total: 0, items: [] },
      ],
    })))
    renderApp('/board')
    const card = (await within(await screen.findByRole('region', { name: 'Not applied' })).findByText('Engineer 1')).closest('li') as HTMLElement
    const data = new Map<string, string>()
    const dataTransfer = { setData: (k: string, v: string) => data.set(k, v), getData: (k: string) => data.get(k) ?? '', effectAllowed: '', dropEffect: '' }
    const column = screen.getByRole('region', { name: 'Interview' })
    fireEvent.dragStart(card, { dataTransfer }); fireEvent.dragOver(column, { dataTransfer }); fireEvent.drop(column, { dataTransfer }); fireEvent.dragEnd(card, { dataTransfer })
    await userEvent.click(await screen.findByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(api.undos).toHaveLength(1))
    expect(api.undos[0].body).toEqual({ expected_status: 'interview', event_id: 7 })
  })
})

describe('what the Undo is tied to', () => {
  it('is not offered when the move wrote nothing (the same status again): there is no entry to take back', async () => {
    backend({ pipeline_status: 'applied' }, () => null, null)
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Add a note…' }))
    await user.click(await screen.findByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Marked Applied')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
  })

  it('stays on screen for the whole undo window, not the few seconds a message normally gets', async () => {
    backend({ pipeline_status: 'applied' })
    const spy = vi.spyOn(toast, 'success')
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    await screen.findByText('Marked Offer')
    const marked = spy.mock.calls.find(([message]) => message === 'Marked Offer')
    expect(UNDO_WINDOW_MS).toBeGreaterThanOrEqual(30_000)
    expect(marked?.[1]).toMatchObject({ duration: UNDO_WINDOW_MS, action: { label: 'Undo' } })
    spy.mockRestore()
  })

  it('goes away on the first press, so a double click cannot undo twice', async () => {
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    const undo = await screen.findByRole('button', { name: 'Undo' })
    fireEvent.click(undo)
    fireEvent.click(undo)
    fireEvent.click(undo)
    await waitFor(() => expect(api.undos).toHaveLength(1))
    await new Promise((r) => setTimeout(r, 100))
    expect(api.undos).toHaveLength(1)
  })

  it('reloads the Overview cards after an undo', async () => {
    backend({ pipeline_status: 'applied' })
    let summaries = 0
    server.use(http.get('/api/summary', () => { summaries += 1; return HttpResponse.json({ ...SUMMARY }) }))
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Status: Applied. Change status' }))
    await user.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    const before = summaries
    await screen.findByText('Undone: back to Applied')
    await waitFor(() => expect(summaries).toBeGreaterThan(before - 1))
    expect(summaries).toBeGreaterThanOrEqual(3)          // the first load, after the move, after the undo
  })
})

describe('Undo in the timeline', () => {
  function timeline() {
    server.use(http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id), { application_status: 'applied', pipeline_status: 'applied' }))))
  }

  it('offers it only on the entry that can be undone, and asks first', async () => {
    timeline()
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/board?job=1')
    const history = await screen.findByRole('list', { name: 'Status history' }, { timeout: 5000 })
    const [first, second] = within(history).getAllByRole('listitem')
    expect(within(first).queryByRole('button', { name: 'Undo this change' })).not.toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(within(second).getByRole('button', { name: 'Undo this change' }))
    const confirm = within(second).getByRole('group', { name: 'Confirm the undo' })
    expect(within(confirm).getByRole('button', { name: 'No' })).toHaveFocus()           // the safe choice has the keyboard
    expect(confirm).toHaveTextContent('Put the job back as it was, and remove this entry for good?')
    await user.click(within(confirm).getByRole('button', { name: 'No' }))
    expect(within(second).getByRole('button', { name: 'Undo this change' })).toBeInTheDocument()
    expect(api.undos).toEqual([])
  })

  it('undoes the entry it was shown on, naming it, and reloads the history', async () => {
    timeline()
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/board?job=1')
    const history = await screen.findByRole('list', { name: 'Status history' }, { timeout: 5000 })
    const user = userEvent.setup()
    await user.click(within(within(history).getAllByRole('listitem')[1]).getByRole('button', { name: 'Undo this change' }))
    const eventsReads = api.reads.events
    await user.click(await screen.findByRole('button', { name: 'Yes, undo' }))
    await waitFor(() => expect(api.undos).toHaveLength(1))
    expect(api.undos[0].body).toEqual({ expected_status: 'applied', event_id: 2 })      // the entry the button belonged to
    expect(await screen.findByText('Undone: back to Applied')).toBeInTheDocument()
    await waitFor(() => expect(api.reads.events).toBeGreaterThan(eventsReads))
    expect(screen.queryByRole('group', { name: 'Confirm the undo' })).not.toBeInTheDocument()
  })

  it('sends one request however often "Yes, undo" is pressed while it is working', async () => {
    timeline()
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    const posts: unknown[] = []
    server.use(http.post('/api/jobs/:id/undo', async ({ request }) => { posts.push(await request.json()); await gate; return HttpResponse.json({ id: 1, status: 'applied' }) }))
    renderApp('/board?job=1')
    const history = await screen.findByRole('list', { name: 'Status history' }, { timeout: 5000 })
    const user = userEvent.setup()
    await user.click(within(within(history).getAllByRole('listitem')[1]).getByRole('button', { name: 'Undo this change' }))
    const yes = await screen.findByRole('button', { name: 'Yes, undo' })
    await user.click(yes)
    await user.click(yes)
    await user.click(yes)
    await waitFor(() => expect(posts).toHaveLength(1))
    release()
  })

  it('shows no undo at all when no entry can be undone', async () => {
    timeline()
    server.use(http.get('/api/jobs/:id/events', () => HttpResponse.json(EVENTS.map((e) => ({ ...e, undoable: false })))))
    renderApp('/board?job=1')
    const history = await screen.findByRole('list', { name: 'Status history' }, { timeout: 5000 })
    expect(within(history).queryByRole('button', { name: /Undo/ })).not.toBeInTheDocument()
  })
})
