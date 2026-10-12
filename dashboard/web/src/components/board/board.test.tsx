import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { describeEvent, responseRate } from '@/lib/board'
import { locationOf, renderApp } from '@/test/render'
import { BOARD, boardCard, jobDetail, server, STATS } from '@/test/server'

describe('what the timeline says', () => {
  it('names the change, and who made it only when it was the dashboard', () => {
    expect(describeEvent({ from_status: null, to_status: 'drafted', source: null })).toBe('Drafted, waiting for review')
    expect(describeEvent({ from_status: 'drafted', to_status: 'applied', source: 'dashboard' })).toBe('Drafted, waiting for review to Applied on the dashboard')
    expect(describeEvent({ from_status: 'offer', to_status: 'offer', source: null })).toBe('Note added')
    expect(describeEvent({ from_status: null, to_status: 'applied', source: 'backfill' })).toBe('Applied')
  })
})

describe('the response rate', () => {
  it('is a whole percent, and a dash before anything is applied (never NaN)', () => {
    expect(responseRate({ applied: 8, responded: 3 })).toBe('38%')
    expect(responseRate({ applied: 3, responded: 3 })).toBe('100%')
    expect(responseRate({ applied: 0, responded: 0 })).toBe('—')
  })
})

/** Records every status change the board sends, and answers like the API. */
function recordMoves(reply: () => Response = () => HttpResponse.json({ id: 1, status: 'applied' })) {
  const moves: { id: string; body: Record<string, unknown>; header: string | null }[] = []
  server.use(http.patch('/api/jobs/:id/status', async ({ request, params }) => {
    moves.push({ id: String(params.id), body: (await request.json()) as Record<string, unknown>, header: request.headers.get('x-requested-with') })
    return reply()
  }))
  return moves
}

/** The browser's drag sequence. fireEvent runs each step inside act, so React has re-rendered before the next one, as it does between real events. */
function dragTo(card: HTMLElement, column: HTMLElement): { dropAllowed: boolean } {
  const data = new Map<string, string>()
  const dataTransfer = { setData: (k: string, v: string) => data.set(k, v), getData: (k: string) => data.get(k) ?? '', effectAllowed: '', dropEffect: '' }
  fireEvent.dragStart(card, { dataTransfer })
  // fireEvent returns false when the handler cancelled the event: the browser only fires `drop` on a target that did
  const dropAllowed = !fireEvent.dragOver(column, { dataTransfer })
  fireEvent.drop(column, { dataTransfer })
  fireEvent.dragEnd(card, { dataTransfer })
  return { dropAllowed }
}

const column = (name: string) => screen.getByRole('region', { name })
/** A card on the board (the follow-up list above it names some of the same jobs, so look inside the column). */
async function cardIn(columnName: string, title: string): Promise<HTMLElement> {
  await screen.findByRole('list', { name: 'Board columns' })
  return (await within(column(columnName)).findByText(title)).closest('li') as HTMLElement
}

describe('the board page', () => {
  it('is reached from the header and shows every column with its true total', async () => {
    const { router } = renderApp('/')
    await userEvent.click(await screen.findByRole('link', { name: 'Board' }))
    expect(await screen.findByRole('heading', { name: 'Board' }, { timeout: 5000 })).toBeInTheDocument()
    expect(locationOf(router)).toBe('/board')
    expect(screen.getByRole('link', { name: 'Board' })).toHaveAttribute('aria-current', 'page')
    const names = (await screen.findAllByRole('heading', { level: 3 })).map((h) => h.textContent)
    expect(names).toEqual(['Follow up (2)', 'Not applied', 'Applied', 'In progress', 'Interview', 'Offer', 'Rejected'])
    expect(column('Not applied')).toHaveTextContent('43 jobs')
    expect(within(column('Not applied')).getByText('Showing 2 of 43. The Jobs page has the rest.')).toBeInTheDocument()
    expect(within(column('Interview')).getByText('Engineer 4')).toBeInTheDocument()
    expect(within(column('Offer')).getByText(/Nothing here yet/)).toBeInTheDocument()
    expect(screen.getByText(/2 withdrawn and 1 skipped/)).toBeInTheDocument()
  })

  it('shows the numbers, with a dash and an honest hint until a reply has been measured', async () => {
    server.use(http.get('/api/stats', () => HttpResponse.json({ ...STATS, avg_days_to_reply: null, replies_measured: 0 })))
    renderApp('/board')
    const numbers = await screen.findByRole('group', { name: 'Application numbers' }).catch(() => screen.getByLabelText('Application numbers'))
    expect(numbers).toHaveTextContent('Heard back')
    expect(numbers).toHaveTextContent('38%')
    expect(numbers).toHaveTextContent('3 of 8 answered')
    expect(numbers).toHaveTextContent('measured from now on')
  })

  it('lists the last 12 weeks, newest first, in a collapsed table', async () => {
    renderApp('/board')
    const summary = await screen.findByText('Week by week')
    await userEvent.click(summary)
    const rows = within(screen.getByRole('table')).getAllByRole('row')
    expect(rows).toHaveLength(13)                       // the header and 12 weeks
    expect(within(rows[1]).getAllByRole('cell').map((c) => c.textContent)).toEqual(['31', '3', '2'])   // the newest week
  })

  it('moves a card dropped on a column, sending the status it showed', async () => {
    const moves = recordMoves()
    renderApp('/board')
    const card = await cardIn('Not applied', 'Engineer 1')
    dragTo(card, column('Interview'))
    await waitFor(() => expect(moves).toHaveLength(1))
    expect(moves[0]).toMatchObject({ id: '1', body: { status: 'interview', expected_status: 'shortlisted' }, header: expect.any(String) })
  })

  it('does nothing when a card is dropped on its own column or on Not applied', async () => {
    const moves = recordMoves()
    renderApp('/board')
    const card = await cardIn('Applied', 'Engineer 3')
    dragTo(card, column('Applied'))
    dragTo(card, column('Not applied'))
    await new Promise((r) => setTimeout(r, 50))
    expect(moves).toEqual([])
  })

  it('shows the reload message when the job changed elsewhere, and does not overwrite', async () => {
    const moves = recordMoves(() => HttpResponse.json({ detail: 'the status was changed elsewhere; reload to see it', current_status: 'rejected' }, { status: 409 }))
    renderApp('/board')
    const card = await cardIn('Applied', 'Engineer 3')
    dragTo(card, column('Offer'))
    expect(await screen.findByText('Changed elsewhere, now Rejected. Reloaded.')).toBeInTheDocument()
    expect(moves).toHaveLength(1)
  })

  it('moves a card with the status menu too (the way on a phone and with a keyboard)', async () => {
    const moves = recordMoves()
    renderApp('/board')
    const card = await cardIn('Not applied', 'Engineer 1')
    await userEvent.click(within(card).getByRole('button', { name: 'Status: Not applied. Change status' }))
    await userEvent.click(await screen.findByRole('menuitemradio', { name: 'Offer' }))
    await waitFor(() => expect(moves).toHaveLength(1))
    expect(moves[0].body).toMatchObject({ status: 'offer', expected_status: 'shortlisted' })
  })

  it('opens the job with its history from a card, and closing it keeps the board', async () => {
    server.use(http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id), { application_status: 'applied', pipeline_status: 'applied' }))))
    const { router } = renderApp('/board')
    await userEvent.click(within(await screen.findByRole('list', { name: 'Applied jobs' })).getByRole('button', { name: 'Engineer 3' }))
    const dialog = await screen.findByRole('dialog', { name: 'Engineer 3' })
    expect(locationOf(router)).toBe('/board?job=3')
    const history = await within(dialog).findByRole('list', { name: 'Status history' })
    expect(within(history).getAllByRole('listitem')).toHaveLength(2)
    expect(history).toHaveTextContent('Drafted, waiting for review to Applied on the dashboard')
    expect(history).toHaveTextContent('sent my CV')
    expect(within(dialog).queryByRole('navigation', { name: 'Jobs on this page' })).not.toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(locationOf(router)).toBe('/board'))
  })

  it('says so when the board or the numbers cannot be loaded, and offers a retry', async () => {
    server.use(
      http.get('/api/board', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })),
      http.get('/api/stats', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })),
    )
    renderApp('/board')
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent).join(' ')).toContain('The board could not be loaded.')
    expect(alerts.map((a) => a.textContent).join(' ')).toContain('The numbers could not be loaded.')
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(2)
  })

  it('keeps the screen-reader-only text inside the scrolling board, so the page itself never scrolls sideways', async () => {
    renderApp('/board')
    const scroller = (await screen.findByRole('list', { name: 'Board columns' })).parentElement as HTMLElement
    expect(scroller.className).toContain('overflow-x-auto')
    expect(scroller.className).toMatch(/(^|\s)relative(\s|$)/)      // an absolutely placed child is clipped by this box, not by the page
  })

  it('accepts a drop only on a column that can take it (the browser needs dragover cancelled for that)', async () => {
    recordMoves()
    renderApp('/board')
    const card = await cardIn('Applied', 'Engineer 3')
    expect(card).toHaveAttribute('draggable', 'true')
    expect(dragTo(card, column('Applied')).dropAllowed).toBe(false)        // where it already is
    expect(dragTo(card, column('Not applied')).dropAllowed).toBe(false)
    expect(dragTo(card, column('Interview')).dropAllowed).toBe(true)
  })

  it('holds a card while it is being saved, and reloads the board, the numbers and the history afterwards', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => { release = resolve })
    const moves: unknown[] = []
    const reads = { board: 0, stats: 0 }
    server.use(
      http.get('/api/board', () => { reads.board += 1; return HttpResponse.json(BOARD) }),
      http.get('/api/stats', () => { reads.stats += 1; return HttpResponse.json(STATS) }),
      http.patch('/api/jobs/:id/status', async ({ request }) => { moves.push(await request.json()); await gate; return HttpResponse.json({ id: 1, status: 'interview' }) }),
    )
    renderApp('/board')
    const card = await cardIn('Not applied', 'Engineer 1')
    await waitFor(() => expect(reads).toEqual({ board: 1, stats: 1 }))
    dragTo(card, column('Interview'))
    await waitFor(() => expect(card).toHaveAttribute('aria-busy', 'true'))
    expect(card).toHaveAttribute('draggable', 'false')
    dragTo(card, column('Offer'))                                    // a second drop on the card in flight is ignored
    expect(moves).toHaveLength(1)
    release()
    await waitFor(() => expect(reads.board).toBe(2))
    expect(reads.stats).toBe(2)
    expect(card).toHaveAttribute('draggable', 'true')
  })

  it('does not start a card drag from inside the note box', async () => {
    renderApp('/board')
    const card = await cardIn('Not applied', 'Engineer 1')
    const inner = within(card).getByRole('button', { name: 'Status: Not applied. Change status' })
    fireEvent.dragStart(inner, { dataTransfer: { setData: () => undefined, effectAllowed: '' } })
    expect(screen.queryByText(/Drop to mark/)).not.toBeInTheDocument()
  })

  it('never asks for the jobs list on the board (the panel has no "next job on this page" there)', async () => {
    const listed: string[] = []
    server.use(http.get('/api/jobs', () => { listed.push('list'); return HttpResponse.json({ items: [], total: 0, page: 1, page_size: 50 }) }))
    server.use(http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id), { application_status: 'applied', pipeline_status: 'applied' }))))
    renderApp('/board?job=3')
    await screen.findByRole('dialog', { name: 'Engineer 3' })
    await new Promise((r) => setTimeout(r, 100))
    expect(listed).toEqual([])
  })

  it('shows a card without a score or place without a gap', async () => {
    server.use(http.get('/api/board', () => HttpResponse.json({
      ...BOARD, columns: [{ key: 'not_applied', total: 1, items: [boardCard(9, { score: null, company: null, location: null, closing_soon: true, deadline: '2026-10-14' })] }, ...BOARD.columns.slice(1)],
    })))
    renderApp('/board')
    const card = await cardIn('Not applied', 'Engineer 9')
    expect(within(card).queryByTitle('Score')).not.toBeInTheDocument()
    expect(card).toHaveTextContent('Closing soon')
  })
})
