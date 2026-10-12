import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, setUnauthorizedHandler, toQuery } from '@/api/client'
import { server } from '@/test/server'

afterEach(() => setUnauthorizedHandler(() => {}))

function capture(method: 'get' | 'post' | 'patch', path: string, reply: () => Response = () => HttpResponse.json({ ok: true })) {
  const seen: Request[] = []
  server.use(http[method](path, ({ request }) => {
    seen.push(request.clone())
    return reply()
  }))
  return seen
}

describe('api()', () => {
  it('reads JSON without the write header', async () => {
    const seen = capture('get', '/api/summary')
    expect(await api('/api/summary')).toEqual({ ok: true })
    expect(seen[0].headers.get('X-Requested-With')).toBeNull()
    expect(seen[0].credentials).toBe('same-origin')
  })

  it('sends the write header and JSON on a change', async () => {
    const seen = capture('patch', '/api/jobs/1/status')
    await api('/api/jobs/1/status', { method: 'PATCH', json: { status: 'applied' } })
    expect(seen[0].headers.get('X-Requested-With')).toBe('jobsearch-dashboard')
    expect(seen[0].headers.get('Content-Type')).toBe('application/json')
    expect(await seen[0].json()).toEqual({ status: 'applied' })
  })

  it('sends the write header even without a body', async () => {
    const seen = capture('post', '/api/logout', () => new HttpResponse(null, { status: 204 }))
    expect(await api('/api/logout', { method: 'POST' })).toBeUndefined()
    expect(seen[0].headers.get('X-Requested-With')).toBe('jobsearch-dashboard')
    expect(seen[0].headers.get('Content-Type')).toBeNull()
  })

  it('turns an error body into an ApiError with the detail and the current status', async () => {
    capture('patch', '/api/jobs/1/status', () =>
      HttpResponse.json({ detail: 'the status was changed elsewhere', current_status: 'interview' }, { status: 409 }))
    const error = await api('/api/jobs/1/status', { method: 'PATCH', json: {} }).catch((e) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, detail: 'the status was changed elsewhere', currentStatus: 'interview' })
  })

  it("joins FastAPI's validation messages", async () => {
    capture('get', '/api/jobs', () =>
      HttpResponse.json({ detail: [{ loc: ['query', 'sort'], msg: 'Input should be score' }, { msg: 'second' }] }, { status: 422 }))
    await expect(api('/api/jobs')).rejects.toMatchObject({ status: 422, detail: 'Input should be score; second' })
  })

  it('reads Retry-After', async () => {
    capture('post', '/api/login', () =>
      HttpResponse.json({ detail: 'too many attempts' }, { status: 429, headers: { 'Retry-After': '840' } }))
    await expect(api('/api/login', { method: 'POST', json: {} })).rejects.toMatchObject({ status: 429, retryAfter: 840 })
  })

  it('has a fallback message for bodies that are not JSON', async () => {
    capture('get', '/api/summary', () => new HttpResponse('Bad gateway', { status: 502 }))
    await expect(api('/api/summary')).rejects.toMatchObject({ status: 502, detail: 'The dashboard API had a problem.' })
  })

  it('reports an unreachable server', async () => {
    server.use(http.get('/api/summary', () => HttpResponse.error()))
    await expect(api('/api/summary')).rejects.toMatchObject({ status: 0, detail: expect.stringContaining('SSH tunnel') })
  })

  it('calls the unauthorized handler on a 401 from any route but login and logout', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    capture('get', '/api/summary', () => HttpResponse.json({ detail: 'x' }, { status: 401 }))
    capture('post', '/api/login', () => HttpResponse.json({ detail: 'wrong password' }, { status: 401 }))
    await expect(api('/api/summary')).rejects.toMatchObject({ status: 401 })
    expect(handler).toHaveBeenCalledTimes(1)
    await expect(api('/api/login', { method: 'POST', json: {} })).rejects.toMatchObject({ detail: 'wrong password' })
    expect(handler).toHaveBeenCalledTimes(1)
  })
})

describe('toQuery()', () => {
  it('repeats keys for lists and leaves out empty values', () => {
    expect(toQuery({ tab: 'all', status: ['interview', 'offer'], q: '', min_score: null, remote: false, page: 2 }))
      .toBe('?tab=all&status=interview&status=offer&remote=false&page=2')
  })

  it('encodes search text', () => {
    expect(toQuery({ q: 'C++ & Go 100%' })).toBe('?q=C%2B%2B+%26+Go+100%25')
  })

  it('is empty for no parameters', () => {
    expect(toQuery({})).toBe('')
  })
})
