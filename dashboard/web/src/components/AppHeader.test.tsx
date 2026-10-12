import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { locationOf, renderApp } from '@/test/render'
import { server, SUMMARY } from '@/test/server'

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()

describe('AppHeader', () => {
  it('says when the numbers were computed, with the exact time on hover', async () => {
    const at = minutesAgo(3)
    server.use(http.get('/api/summary', () => HttpResponse.json({ ...SUMMARY, generated_at: at })))
    renderApp('/')
    const time = await screen.findByText('3m ago')
    expect(time.tagName).toBe('TIME')
    expect(time).toHaveAttribute('dateTime', at)
    expect(time.getAttribute('title')).toMatch(/^\d{1,2} \w{3} \d{4}, \d{2}:\d{2}$/)
    expect(time.parentElement).toHaveTextContent('Updated 3m ago')
  })

  it('never says "in 1m" when the server clock runs ahead', async () => {
    server.use(http.get('/api/summary', () => HttpResponse.json({ ...SUMMARY, generated_at: minutesAgo(-2) })))
    renderApp('/')
    expect(await screen.findByText('just now')).toBeInTheDocument()
  })

  it('refetches the summary on Refresh', async () => {
    let calls = 0
    server.use(http.get('/api/summary', () => { calls++; return HttpResponse.json(SUMMARY) }))
    renderApp('/')
    await screen.findByRole('button', { name: /^Jobs found/ })
    expect(calls).toBe(1)
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByRole('button', { name: 'Refresh' })
    await expect.poll(() => calls).toBe(2)
  })

  it('exports what the table shows, every page', async () => {
    renderApp('/?tab=all&q=acme&source=lever&sort=company&order=asc&page=3&page_size=100&job=9')
    // the open job's panel hides the rest of the page from assistive technology
    const link = await screen.findByRole('link', { name: 'Export CSV', hidden: true })
    expect(link).toHaveAttribute('href', '/api/export.csv?tab=all&q=acme&source=lever&sort=company&order=asc')
    expect(link).toHaveAttribute('download')
  })

  it('follows a card click in the export link', async () => {
    renderApp('/')
    await userEvent.click(await screen.findByRole('button', { name: /^Interview/ }))
    expect(screen.getByRole('link', { name: 'Export CSV' }))
      .toHaveAttribute('href', '/api/export.csv?tab=applied&status=interview')
  })

  it('logs out with the write header and goes to the login page', async () => {
    let header: string | null = null
    server.use(http.post('/api/logout', ({ request }) => {
      header = request.headers.get('X-Requested-With')
      return new HttpResponse(null, { status: 204 })
    }))
    const { router, client } = renderApp('/?tab=applied')
    await userEvent.click(await screen.findByRole('button', { name: 'Log out' }))
    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(locationOf(router)).toBe('/login')
    await waitFor(() => expect(header).toBe('jobsearch-dashboard'))
    await waitFor(() => expect(client.getQueryData(['summary'])).toBeUndefined())
    expect(client.getQueryData(['me'])).toBeUndefined()
  })

  it('sends nothing more for the overview after logging out', async () => {
    let summaries = 0
    let done = false
    server.use(
      http.get('/api/summary', () => { summaries++; return HttpResponse.json(SUMMARY) }),
      http.post('/api/logout', async () => { await new Promise((r) => setTimeout(r, 50)); done = true; return new HttpResponse(null, { status: 204 }) }),
    )
    renderApp('/')
    await screen.findByRole('img', { name: /^Applications by status/ })
    await userEvent.click(screen.getByRole('button', { name: 'Log out' }))
    await waitFor(() => expect(done).toBe(true))
    await new Promise((r) => setTimeout(r, 50))
    expect(summaries).toBe(1)
  })

  it('still leaves when the logout request fails', async () => {
    server.use(http.post('/api/logout', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    const { router } = renderApp('/')
    await userEvent.click(await screen.findByRole('button', { name: 'Log out' }))
    await screen.findByLabelText('Password')
    expect(locationOf(router)).toBe('/login')
  })
})
