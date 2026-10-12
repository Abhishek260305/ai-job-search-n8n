import { screen, waitFor } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/render'
import { server, SUMMARY } from '@/test/server'

describe('the charts on the overview', () => {
  it('show both charts from the one summary request', async () => {
    let calls = 0
    server.use(http.get('/api/summary', () => { calls++; return HttpResponse.json(SUMMARY) }))
    renderApp('/')
    // the first render in this file loads the lazy page and chart modules, which can take over a second under load
    expect(await screen.findByRole('img', { name: /^Applications by status:/ }, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /^Where jobs drop out/ })).toBeInTheDocument()
    expect(calls).toBe(1)
  })

  it('show placeholders while loading', async () => {
    server.use(http.get('/api/summary', async () => { await delay('infinite'); return HttpResponse.json(SUMMARY) }))
    renderApp('/')
    expect(await screen.findByRole('status', { name: 'Loading the charts' })).toBeInTheDocument()
  })

  it('leave the error to the cards, so it is reported once', async () => {
    server.use(http.get('/api/summary', () => HttpResponse.json({ detail: 'the database is busy' }, { status: 503 })))
    renderApp('/')
    expect(await screen.findAllByRole('alert')).toHaveLength(1)
    await waitFor(() => expect(screen.queryByRole('status', { name: 'Loading the charts' })).not.toBeInTheDocument())
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })
})
