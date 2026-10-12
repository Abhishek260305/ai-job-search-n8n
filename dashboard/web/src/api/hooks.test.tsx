import { QueryClient } from '@tanstack/react-query'
import { act, renderHook, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { keys, useJobs, useLogout, useSetStatus } from '@/api/hooks'
import { AppProviders } from '@/AppProviders'
import { server } from '@/test/server'

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <AppProviders client={client}>{children}</AppProviders>
  return { client, wrapper }
}

function seedCache(client: QueryClient) {
  client.setQueryData(keys.summary, { cards: {} })
  client.setQueryData(keys.jobs({ tab: 'all' }), { items: [] })
  client.setQueryData(keys.job(5), { id: 5 })
  client.setQueryData(keys.job(6), { id: 6 })
}

const stale = (client: QueryClient, key: readonly unknown[]) => client.getQueryState(key)?.isInvalidated

describe('useJobs', () => {
  it('asks for the page with repeated list parameters', async () => {
    let url = ''
    server.use(http.get('/api/jobs', ({ request }) => {
      url = request.url
      return HttpResponse.json({ items: [], total: 0, page: 2, page_size: 25 })
    }))
    const { wrapper } = setup()
    const { result } = renderHook(() => useJobs({ tab: 'all', status: ['interview', 'offer'], page: 2, page_size: 25 }), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(new URL(url).search).toBe('?tab=all&status=interview&status=offer&page=2&page_size=25')
  })
})

describe('useSetStatus', () => {
  it('sends the change and refreshes the summary, the table and that job only', async () => {
    let body: unknown
    server.use(http.patch('/api/jobs/5/status', async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ id: 5, status: 'interview' })
    }))
    const { client, wrapper } = setup()
    seedCache(client)
    const { result } = renderHook(() => useSetStatus(), { wrapper })
    await act(() => result.current.mutateAsync({ id: 5, status: 'interview', expected: 'applied', note: '  phone screen ' }))
    expect(body).toEqual({ status: 'interview', expected_status: 'applied', note: 'phone screen' })
    expect(stale(client, keys.summary)).toBe(true)
    expect(stale(client, keys.jobs({ tab: 'all' }))).toBe(true)
    expect(stale(client, keys.job(5))).toBe(true)
    expect(stale(client, keys.job(6))).toBe(false)
    expect(await screen.findByText('Marked Interview')).toBeInTheDocument()
  })

  it('leaves a blank note out', async () => {
    let body: unknown
    server.use(http.patch('/api/jobs/5/status', async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ id: 5, status: 'applied' })
    }))
    const { wrapper } = setup()
    const { result } = renderHook(() => useSetStatus(), { wrapper })
    await act(() => result.current.mutateAsync({ id: 5, status: 'applied', expected: 'shortlisted', note: '   ' }))
    expect(body).toEqual({ status: 'applied', expected_status: 'shortlisted' })
  })

  it('on a conflict, reloads and says what the status is now', async () => {
    server.use(http.patch('/api/jobs/5/status', () =>
      HttpResponse.json({ detail: 'the status was changed elsewhere', current_status: 'shortlisted' }, { status: 409 })))
    const { client, wrapper } = setup()
    seedCache(client)
    const { result } = renderHook(() => useSetStatus(), { wrapper })
    act(() => result.current.mutate({ id: 5, status: 'interview', expected: 'applied' }))
    expect(await screen.findByText('Changed elsewhere, now Not applied. Reloaded.')).toBeInTheDocument()
    expect(stale(client, keys.summary)).toBe(true)
    expect(stale(client, keys.job(5))).toBe(true)
  })

  it('shows the reason for other refusals', async () => {
    server.use(http.patch('/api/jobs/5/status', () => HttpResponse.json({ detail: 'job 5 is scam-blocked' }, { status: 403 })))
    const { wrapper } = setup()
    const { result } = renderHook(() => useSetStatus(), { wrapper })
    act(() => result.current.mutate({ id: 5, status: 'applied', expected: 'scam_blocked' }))
    expect(await screen.findByText('job 5 is scam-blocked')).toBeInTheDocument()
  })
})

describe('useLogout', () => {
  it('forgets every cached answer, even if the request fails', async () => {
    server.use(http.post('/api/logout', () => HttpResponse.error()))
    const { client, wrapper } = setup()
    seedCache(client)
    const { result } = renderHook(() => useLogout(), { wrapper })
    await act(() => result.current.mutateAsync().catch(() => undefined))
    expect(client.getQueryData(keys.summary)).toBeUndefined()
    expect(client.getQueryData(keys.job(5))).toBeUndefined()
  })
})
