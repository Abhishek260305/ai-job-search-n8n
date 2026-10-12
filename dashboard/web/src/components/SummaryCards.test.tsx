import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { locationOf, renderApp } from '@/test/render'
import { server, SUMMARY } from '@/test/server'

const card = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name}`) })
const valueOf = (name: string) => within(card(name)).getByText(/^[\d,]+$/).textContent

describe('SummaryCards', () => {
  it('shows the eight numbers in order', async () => {
    renderApp('/')
    await screen.findByRole('button', { name: /^Jobs found/ })
    const labels = within(screen.getByRole('region', { name: 'Summary' })).getAllByRole('button')
      .map((b) => b.firstChild?.textContent)
    expect(labels).toEqual(['Jobs found', 'Shortlisted', 'New this week', 'Applied', 'In progress', 'Interview', 'Offer', 'Rejected'])
    expect(valueOf('Jobs found')).toBe('1,234')
    expect(valueOf('Shortlisted')).toBe('43')
    expect(valueOf('New this week')).toBe('7')
    expect(valueOf('Applied')).toBe('5')
    expect(valueOf('In progress')).toBe('2')
    expect(valueOf('Interview')).toBe('1')
    expect(valueOf('Rejected')).toBe('2')
  })

  it('shows 0, not a blank, for an empty stage', async () => {
    server.use(http.get('/api/summary', () =>
      HttpResponse.json({ ...SUMMARY, cards: { ...SUMMARY.cards, found: 0, applied: 0, interview: 0 } })))
    renderApp('/')
    await screen.findByRole('button', { name: /^Jobs found/ })
    expect(valueOf('Offer')).toBe('0')
    expect(valueOf('Jobs found')).toBe('0')
    expect(valueOf('Interview')).toBe('0')
  })

  it('names the threshold behind the shortlist', async () => {
    renderApp('/')
    expect(await screen.findByText('score ≥ 60, not acted on yet')).toBeInTheDocument()
  })

  it('does without a threshold', async () => {
    server.use(http.get('/api/summary', () => HttpResponse.json({ ...SUMMARY, threshold: null })))
    renderApp('/')
    expect(await screen.findByText('not acted on yet')).toBeInTheDocument()
  })

  it.each([
    ['Interview', '/?tab=applied&status=interview'],
    ['In progress', '/?tab=applied&status=in_progress'],
    ['Offer', '/?tab=applied&status=offer'],
    ['Rejected', '/?tab=applied&status=rejected'],
    ['Applied', '/?tab=applied'],
    ['Jobs found', '/?tab=all'],
    ['Shortlisted', '/?status=shortlisted'],
    ['New this week', '/?status=shortlisted&sort=first_seen'],
  ])('clicking %s opens %s', async (name, expected) => {
    const { router } = renderApp('/')
    await screen.findByRole('button', { name: /^Jobs found/ })
    await userEvent.click(card(name))
    expect(locationOf(router)).toBe(expected)
    expect(card(name)).toHaveAttribute('aria-pressed', 'true')
  })

  it('starts a fresh view, keeping only the page size', async () => {
    const { router } = renderApp('/?tab=all&q=acme&source=lever&remote=true&sort=company&page=4&page_size=100')
    await screen.findByRole('button', { name: /^Jobs found/ })
    await userEvent.click(card('Offer'))
    expect(locationOf(router)).toBe('/?tab=applied&status=offer&page_size=100')
  })

  it('marks the card that matches the view in the URL, and only that one', async () => {
    renderApp('/?tab=applied&status=interview&page=2')
    await screen.findByRole('button', { name: /^Jobs found/ })
    const pressed = within(screen.getByRole('region', { name: 'Summary' })).getAllByRole('button', { pressed: true })
    expect(pressed).toEqual([card('Interview')])
  })

  it('marks no card for a view no card opens', async () => {
    renderApp('/?tab=applied&status=interview&q=acme')
    await screen.findByRole('button', { name: /^Jobs found/ })
    expect(within(screen.getByRole('region', { name: 'Summary' })).queryAllByRole('button', { pressed: true })).toEqual([])
  })

  it('shows skeletons while loading', async () => {
    server.use(http.get('/api/summary', async () => { await delay('infinite'); return HttpResponse.json(SUMMARY) }))
    renderApp('/')
    expect(await screen.findByRole('status', { name: 'Loading the summary' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Jobs found/ })).not.toBeInTheDocument()
  })

  it('shows an error with a retry that works', async () => {
    let calls = 0
    server.use(http.get('/api/summary', () =>
      ++calls === 1 ? HttpResponse.json({ detail: 'the database is busy' }, { status: 503 }) : HttpResponse.json(SUMMARY)))
    renderApp('/')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The summary could not be loaded.')
    expect(alert).toHaveTextContent('the database is busy')
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: /^Jobs found/ })).toBeInTheDocument()
    expect(calls).toBe(2)
  })
})
