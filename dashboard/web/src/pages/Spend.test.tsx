import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { Spend } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { server, SPEND, unauthorized } from '@/test/server'

const spend = (over: Partial<Spend> = {}) => server.use(http.get('/api/spend', () => HttpResponse.json({ ...SPEND, ...over })))

describe('the spend page', () => {
  it('is reached from the header and shows each cap with what is left', async () => {
    const { router } = renderApp('/')
    await userEvent.click(await screen.findByRole('link', { name: 'Spend' }))
    expect(await screen.findByRole('heading', { name: 'Spend' }, { timeout: 5000 })).toBeInTheDocument()
    expect(locationOf(router)).toBe('/spend')
    const meters = await screen.findByRole('region', { name: 'Spend against the caps' }, { timeout: 5000 })
    const month = within(meters).getByRole('progressbar', { name: 'Claude, this month: $0.53 of $10.00' })
    expect(month).toHaveAttribute('aria-valuenow', '5')
    expect(month).toHaveAttribute('aria-valuetext', '5% used')
    expect(within(meters).getByRole('progressbar', { name: 'Claude, today: $0.02 of $1.50' })).toBeInTheDocument()
    expect(within(meters).getByRole('progressbar', { name: 'Apify, this month: $0.53 of $15.00' })).toBeInTheDocument()
    expect(within(meters).getByRole('progressbar', { name: 'Apify, today: $0.00 of $0.50' })).toBeInTheDocument()
    expect(meters).toHaveTextContent('$9.47 left')
    expect(screen.queryByText('Cap reached')).not.toBeInTheDocument()
  })

  it('says when a cap is reached, in words, and how it clears', async () => {
    spend({
      claude: { ...SPEND.claude, day_spent: 1.5, day_remaining: 0, blocked: true, blocked_reason: 'daily cap reached' },
      apify: { ...SPEND.apify, day_spent: 0.55, day_remaining: 0, blocked: true, blocked_reason: 'Apify daily cap reached' },
    })
    renderApp('/spend')
    expect(await screen.findAllByText('Cap reached')).toHaveLength(2)
    const note = screen.getByRole('status')
    expect(note).toHaveTextContent('Claude: daily cap reached. Apify daily cap reached.')
    expect(note).toHaveTextContent('scripts/jobsearch budget set')
    expect(screen.getByRole('progressbar', { name: /^Claude, today/ })).toHaveAttribute('aria-valuenow', '100')
  })

  it('turns amber near the monthly cap', async () => {
    spend({ claude: { ...SPEND.claude, month_spent: 8.5, month_remaining: 1.5, warn: true } })
    renderApp('/spend')
    expect(await screen.findByText('Nearly there')).toBeInTheDocument()
  })

  it('describes the bars in words and lists the numbers by day', async () => {
    renderApp('/spend')
    const chart = await screen.findByRole('img', { name: /^Spending per day over the last 30 days/ })
    expect(chart).toHaveAccessibleName(expect.stringContaining('$0.996 in total'))
    expect(chart).toHaveAccessibleName(expect.stringContaining('Scoring, Tailoring, Apify'))
    expect(chart.querySelectorAll('rect').length).toBe(4)
    expect(within(screen.getByRole('list', { name: 'Legend' })).getAllByRole('listitem')).toHaveLength(3)
    await userEvent.click(screen.getByText('Show the numbers'))
    const table = screen.getByRole('table')
    const rows = within(table).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('12 Oct')           // newest first
    expect(rows[2]).toHaveTextContent('11 Oct')
    expect(rows[2]).toHaveTextContent('$0.24')
    expect(rows[2]).toHaveTextContent('$0.98')            // 0.24 + 0.21 + 0.53 that day
  })

  it('shows an empty chart and no table when nothing was spent', async () => {
    spend({ daily: [], models: [] })
    renderApp('/spend')
    expect(await screen.findByRole('img', { name: 'No spending in the last 30 days.' })).toBeInTheDocument()
    expect(screen.queryByText('Show the numbers')).not.toBeInTheDocument()
    expect(screen.getByText('Nothing has been spent this month.')).toBeInTheDocument()
  })

  it('lists the models with calls, tokens and cost', async () => {
    renderApp('/spend')
    const models = await screen.findByRole('list', { name: 'Models' })
    const [claude, apify] = within(models).getAllByRole('listitem')
    expect(claude).toHaveTextContent('claude-haiku-5-5')
    expect(claude).toHaveTextContent('640 calls · 900,000 in, 80,000 out')
    expect(claude).toHaveTextContent('$0.256')
    expect(apify).toHaveTextContent('Apify')
    expect(apify).not.toHaveTextContent('in,')
  })

  it('reports a failed load with a retry, and sends an ended session to the login page', async () => {
    server.use(http.get('/api/spend', () => HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })))
    renderApp('/spend')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The spend report could not be loaded.')
    server.use(http.get('/api/spend', () => HttpResponse.json(SPEND)))
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('region', { name: 'Spend against the caps' })).toBeInTheDocument()
  })

  it('goes to the login page when the session has ended', async () => {
    server.use(http.get('/api/spend', unauthorized), http.get('/api/me', unauthorized))
    const { router } = renderApp('/spend')
    await waitFor(() => expect(locationOf(router)).toContain('/login'))
  })
})
