import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { locationOf, renderApp } from '@/test/render'
import { server, unauthorized } from '@/test/server'

const PASSWORD = 'correct horse battery'

function acceptPassword() {
  const tries: unknown[] = []
  server.use(http.post('/api/login', async ({ request }) => {
    const body = (await request.json()) as { password: string }
    tries.push(body)
    if (request.headers.get('X-Requested-With') !== 'jobsearch-dashboard') return HttpResponse.json({}, { status: 403 })
    return body.password === PASSWORD
      ? new HttpResponse(null, { status: 204 })
      : HttpResponse.json({ detail: 'wrong password' }, { status: 401 })
  }))
  return tries
}

async function signIn(password = PASSWORD) {
  const field = await screen.findByLabelText('Password')
  await userEvent.clear(field)
  await userEvent.type(field, password)
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

describe('logging in', () => {
  it('goes to the page in next after the right password', async () => {
    acceptPassword()
    const { router } = renderApp('/login?next=%2Fjobs%3Ftab%3Dapplied')
    await signIn()
    await waitFor(() => expect(locationOf(router)).toBe('/jobs?tab=applied'))
  })

  it('goes to the overview without next', async () => {
    const tries = acceptPassword()
    const { router } = renderApp('/login')
    await signIn()
    await waitFor(() => expect(locationOf(router)).toBe('/'))
    expect(await screen.findByRole('heading', { name: 'Job search' })).toBeInTheDocument()
    expect(tries).toEqual([{ password: PASSWORD }])
  })

  it.each(['https://evil.example/', '//evil.example', '/\\evil.example', 'javascript:alert(1)'])(
    'ignores a next pointing elsewhere (%s)', async (next) => {
      acceptPassword()
      const { router } = renderApp(`/login?next=${encodeURIComponent(next)}`)
      await signIn()
      await waitFor(() => expect(locationOf(router)).toBe('/'))
    })

  it('says "Wrong password", stays on the page and clears the field', async () => {
    acceptPassword()
    const { router } = renderApp('/login')
    await signIn('nope')
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong password.')
    expect(locationOf(router)).toBe('/login')
    expect(screen.getByLabelText('Password')).toHaveValue('')
    expect(screen.getByLabelText('Password')).toHaveAttribute('aria-invalid', 'true')
  })

  it('reports the lockout with the time from Retry-After', async () => {
    server.use(http.post('/api/login', () =>
      HttpResponse.json({ detail: 'too many attempts' }, { status: 429, headers: { 'Retry-After': '840' } })))
    renderApp('/login')
    await signIn()
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Try again in 14 minutes.')
  })

  it('cannot be submitted empty', async () => {
    renderApp('/login')
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeDisabled()
  })
})

describe('pages that need a session', () => {
  it('send you to the login page and back afterwards', async () => {
    let loggedIn = false
    server.use(
      http.get('/api/me', () => (loggedIn ? HttpResponse.json({ authenticated: true, expires_at: '2026-10-19T09:30:00Z' }) : unauthorized())),
      http.post('/api/login', () => {
        loggedIn = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { router } = renderApp('/no/such/page?x=1')
    await waitFor(() => expect(locationOf(router)).toBe('/login?next=%2Fno%2Fsuch%2Fpage%3Fx%3D1'))
    await signIn()
    await waitFor(() => expect(locationOf(router)).toBe('/no/such/page?x=1'))
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
  })

  it('show a loading state while the session is checked', async () => {
    server.use(http.get('/api/me', async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
      return HttpResponse.json({ authenticated: true, expires_at: '2026-10-19T09:30:00Z' })
    }))
    renderApp('/')
    expect(screen.getByLabelText('Loading')).toHaveAttribute('aria-busy', 'true')
    expect(await screen.findByRole('heading', { name: 'Job search' })).toBeInTheDocument()
  })

  it('explain an unreachable API and offer a retry', async () => {
    let calls = 0
    server.use(http.get('/api/me', () => {
      calls += 1
      return calls === 1
        ? HttpResponse.json({ detail: 'the database is not available right now' }, { status: 503 })
        : HttpResponse.json({ authenticated: true, expires_at: '2026-10-19T09:30:00Z' })
    }))
    const { router } = renderApp('/')
    expect(await screen.findByRole('alert')).toHaveTextContent('the database is not available right now')
    expect(locationOf(router)).toBe('/')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: 'Job search' })).toBeInTheDocument()
  })
})
