import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { EmailInbox, EmailItem } from '@/api/types'
import { renderApp } from '@/test/render'
import { emailItem, server } from '@/test/server'

function inbox(items: EmailItem[], overrides: Partial<EmailInbox> = {}) {
  const pending = items.filter((i) => i.state === 'pending').length
  const body: EmailInbox = { enabled: true, login: 'ok', counts: { pending, accepted: 1, dismissed: 2 }, items, ...overrides }
  server.use(
    http.get('/api/email/count', () => HttpResponse.json({ enabled: body.enabled, pending: body.counts.pending })),
    http.get('/api/email', ({ request }) => {
      const state = new URL(request.url).searchParams.get('state')
      return HttpResponse.json({ ...body, items: body.items.filter((i) => i.state === state) })
    }),
  )
}

const card = async (name: RegExp | string) => within((await screen.findByRole('heading', { name })).closest('li') as HTMLElement)

describe('the email page', () => {
  it('lists what waits, with the job, the sender, the subject and what accepting would do', async () => {
    inbox([emailItem(1)])
    renderApp('/email')
    const c = await card(/Company 3/)
    expect(c.getByText('company3.com')).toBeInTheDocument()
    expect(c.getByText('Interview invitation')).toBeInTheDocument()
    expect(c.getByText('We would like to schedule a call.')).toBeInTheDocument()
    expect(c.getByText(/Looks like an interview invitation\. Accepting marks this job Interview \(it is Applied now\)\./)).toBeInTheDocument()
    expect(c.getByText('Medium confidence')).toBeInTheDocument()
    expect(c.getByText('Read by rules')).toBeInTheDocument()
    expect(c.getByRole('button', { name: /Mark Interview: accept the suggestion for Company 3/ })).toBeEnabled()
  })

  it('shows an email\'s subject and excerpt as plain text, never as markup or a link', async () => {
    inbox([emailItem(1, { subject: '<img src=x onerror=alert(1)>Hello', excerpt: '<a href="https://evil.example">click</a> [link]' })])
    const { container } = renderApp('/email')
    await card(/Company 3/)
    expect(screen.getByText('<img src=x onerror=alert(1)>Hello')).toBeInTheDocument()
    expect(container.querySelector('img[src="x"]')).toBeNull()
    expect(container.querySelector('a[href="https://evil.example"]')).toBeNull()
  })

  it('says why the list is empty, and offers a way to switch the part on, when it is off', async () => {
    inbox([], { enabled: false })
    renderApp('/email')
    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('The Gmail part is off')
    expect(within(note).getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings')
    expect(screen.getByText(/Nothing to review/)).toBeInTheDocument()
  })

  it('warns when the Google login expired', async () => {
    inbox([], { login: 'expired' })
    renderApp('/email')
    expect(await screen.findByRole('alert')).toHaveTextContent('Google login expired')
    expect(screen.getByRole('alert')).toHaveTextContent('scripts/jobsearch gmail login')
  })

  it('shows the three tabs with their counts and switches between them', async () => {
    inbox([emailItem(1), emailItem(2, { state: 'accepted', decided_at: '2026-10-12T09:00:00Z', event_id: 9, can_undo: true, company: 'Beta' }),
           emailItem(3, { state: 'dismissed', decided_at: '2026-10-12T09:10:00Z', company: 'Gamma', can_restore: true })])
    const user = userEvent.setup()
    renderApp('/email')
    await card(/Company 3/)
    const tabs = screen.getByRole('tablist', { name: 'Suggestions' })
    expect(within(tabs).getByRole('tab', { name: 'To review (1)' })).toBeInTheDocument()
    expect(within(tabs).getByRole('tab', { name: 'Accepted (1)' })).toBeInTheDocument()
    await user.click(within(tabs).getByRole('tab', { name: 'Accepted (1)' }))
    expect(await (await card(/Beta/)).findByRole('button', { name: 'Undo' })).toBeInTheDocument()
    await user.click(within(tabs).getByRole('tab', { name: 'Dismissed (2)' }))
    expect(await (await card(/Gamma/)).findByRole('button', { name: 'Bring back' })).toBeInTheDocument()
  })
})

describe('accepting', () => {
  it('sends the status the page showed and offers Undo, tied to the history entry the change wrote', async () => {
    inbox([emailItem(1)])
    const seen: unknown[] = []
    server.use(
      http.post('/api/email/1/accept', async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: 1, job_id: 3, status: 'interview', event_id: 77 })
      }),
      http.post('/api/jobs/3/undo', async ({ request }) => {
        seen.push(await request.json())
        return HttpResponse.json({ id: 3, status: 'applied' })
      }),
    )
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await (await card(/Company 3/)).findByRole('button', { name: /Mark Interview: accept the suggestion for Company 3/ }))
    await waitFor(() => expect(seen[0]).toEqual({ expected_status: 'applied', acknowledged: false }))
    const toast = await screen.findByText('Marked Interview')
    await user.click(within(toast.closest('[data-sonner-toast]') as HTMLElement).getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(seen[1]).toEqual({ expected_status: 'interview', event_id: 77 }))
  })

  it('asks for a second look when the suggestion carries a trust flag, and will not accept before it', async () => {
    inbox([emailItem(1, { signal: 'offer', proposed_status: 'offer', confidence: 'low', flags: ['phishing_risk', 'unverified_sender'], needs_ack: true })])
    const seen: unknown[] = []
    server.use(http.post('/api/email/1/accept', async ({ request }) => {
      seen.push(await request.json())
      return HttpResponse.json({ id: 1, job_id: 3, status: 'offer', event_id: null })
    }))
    const user = userEvent.setup()
    renderApp('/email')
    const c = await card(/Company 3/)
    expect(c.getByText('Possible phishing: check who sent it')).toBeInTheDocument()
    expect(c.getByText('Google could not verify the sender')).toBeInTheDocument()
    expect(c.getByText('Low confidence')).toBeInTheDocument()
    const accept = c.getByRole('button', { name: /Mark Offer: accept the suggestion for Company 3/ })
    expect(accept).toBeDisabled()
    await user.click(c.getByRole('checkbox', { name: /I checked who sent this/ }))
    expect(accept).toBeEnabled()
    await user.click(accept)
    await waitFor(() => expect(seen).toEqual([{ expected_status: 'applied', acknowledged: true }]))
  })

  it('does not offer Accept for a stale suggestion, and says what the job is now', async () => {
    inbox([emailItem(1, { stale: true, can_accept: false, current_status: 'rejected' })])
    renderApp('/email')
    const c = await card(/Company 3/)
    expect(c.getByRole('note')).toHaveTextContent('This job has moved on (it is Rejected now)')
    expect(c.getByRole('button', { name: /Mark Interview/ })).toBeDisabled()
    expect(c.getByRole('button', { name: /Dismiss/ })).toBeEnabled()
  })

  it('reloads and says why when the job changed meanwhile (409)', async () => {
    inbox([emailItem(1)])
    server.use(http.post('/api/email/1/accept', () => HttpResponse.json({ detail: 'the status was changed elsewhere; reload to see it', current_status: 'rejected' }, { status: 409 })))
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await (await card(/Company 3/)).findByRole('button', { name: /Mark Interview/ }))
    expect(await screen.findByText(/It is Rejected now\. Reloaded\./)).toBeInTheDocument()
  })

  it('shows the database\'s own refusal in words (a scam-blocked job)', async () => {
    inbox([emailItem(1)])
    server.use(http.post('/api/email/1/accept', () => HttpResponse.json({ detail: 'job 3 is scam-blocked' }, { status: 403 })))
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await (await card(/Company 3/)).findByRole('button', { name: /Mark Interview/ }))
    expect(await screen.findByText('job 3 is scam-blocked')).toBeInTheDocument()
  })
})

describe('dismissing and restoring', () => {
  it('dismisses a suggestion', async () => {
    inbox([emailItem(1)])
    let dismissed = 0
    server.use(http.post('/api/email/1/dismiss', () => { dismissed += 1; return HttpResponse.json({ id: 1, state: 'dismissed' }) }))
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await (await card(/Company 3/)).findByRole('button', { name: /Dismiss/ }))
    await waitFor(() => expect(dismissed).toBe(1))
    expect(await screen.findByText('Dismissed')).toBeInTheDocument()
  })

  it('brings a dismissed suggestion back, and says when another is already waiting (409)', async () => {
    inbox([emailItem(3, { state: 'dismissed', decided_at: '2026-10-12T09:10:00Z', company: 'Gamma', can_restore: true })])
    server.use(http.post('/api/email/3/restore', () => HttpResponse.json({ detail: 'Another suggestion for this job and status is already waiting.' }, { status: 409 })))
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await screen.findByRole('tab', { name: /Dismissed/ }))
    await user.click(await (await card(/Gamma/)).findByRole('button', { name: 'Bring back' }))
    expect(await screen.findByText(/Another suggestion for this job and status is already waiting/)).toBeInTheDocument()
  })
})

describe('the menu badge', () => {
  it('shows how many suggestions wait, only while the part is on and something waits', async () => {
    inbox([emailItem(1), emailItem(2)])
    renderApp('/board')
    const nav = await screen.findByRole('navigation', { name: 'Sections' })
    expect(await within(nav).findByText('(2 waiting)')).toBeInTheDocument()
  })

  it('shows nothing while the Gmail part is off or nothing waits', async () => {
    inbox([], { enabled: false })
    renderApp('/board')
    const nav = await screen.findByRole('navigation', { name: 'Sections' })
    await waitFor(() => expect(within(nav).getByRole('link', { name: 'Email' })).toBeInTheDocument())
    expect(within(nav).queryByText(/waiting/)).toBeNull()
  })
})

describe('after an undo, and the review fixes', () => {
  it('says plainly that an accepted suggestion\'s change was undone, and lets it be reviewed again', async () => {
    inbox([emailItem(2, { state: 'accepted', decided_at: '2026-10-12T09:00:00Z', company: 'Beta', current_status: 'applied', proposed_status: 'interview', can_restore: true })])
    let restored = 0
    server.use(http.post('/api/email/2/restore', () => { restored += 1; return HttpResponse.json({ id: 2, state: 'pending' }) }))
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await screen.findByRole('tab', { name: /Accepted/ }))
    const c = await card(/Beta/)
    expect(c.getByText(/marked Interview\./)).toBeInTheDocument()
    expect(c.getByText(/The job is Applied now \(the change was undone, or the job moved on\)\./)).toBeInTheDocument()
    await user.click(c.getByRole('button', { name: 'Review again' }))
    await waitFor(() => expect(restored).toBe(1))
  })

  it('says nothing about the job having changed when it still has the accepted status', async () => {
    inbox([emailItem(2, { state: 'accepted', decided_at: '2026-10-12T09:00:00Z', company: 'Beta', current_status: 'interview', proposed_status: 'interview' })])
    const user = userEvent.setup()
    renderApp('/email')
    await user.click(await screen.findByRole('tab', { name: /Accepted/ }))
    const c = await card(/Beta/)
    expect(c.queryByText(/now \(the change was undone/)).toBeNull()
    expect(c.queryByRole('button', { name: 'Review again' })).toBeNull()
  })

  it('asks for the second look on an unverified sender (the database and the page agree on which flags)', async () => {
    inbox([emailItem(1, { flags: ['unverified_sender'], needs_ack: true })])
    renderApp('/email')
    const c = await card(/Company 3/)
    expect(c.getByText('Google could not verify the sender')).toBeInTheDocument()
    expect(c.getByRole('button', { name: /Mark Interview/ })).toBeDisabled()
  })

  it('forgets a tick given for an email that was replaced in place by a different one', async () => {
    inbox([emailItem(1, { flags: ['unverified_sender'], needs_ack: true })])
    const user = userEvent.setup()
    renderApp('/email')
    const c = await card(/Company 3/)
    await user.click(c.getByRole('checkbox', { name: /I checked who sent this/ }))
    expect(c.getByRole('button', { name: /Mark Interview/ })).toBeEnabled()
    inbox([emailItem(1, { flags: ['unverified_sender'], needs_ack: true, from_domain: 'company3-careers.example', subject: 'A different email' })])
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    const again = await card(/Company 3/)
    await within(await screen.findByText('A different email').then((n) => n.closest('li') as HTMLElement)).findByRole('button', { name: /Mark Interview/ })
    expect(within(screen.getByText('A different email').closest('li') as HTMLElement).getByRole('button', { name: /Mark Interview/ })).toBeDisabled()
    expect(again).toBeTruthy()
  })

  it('says when there are more suggestions than the page shows', async () => {
    inbox([emailItem(1)], { counts: { pending: 130, accepted: 0, dismissed: 0 } })
    renderApp('/email')
    expect(await screen.findByText('Showing the newest 1 of 130. Deal with these and the rest appear.')).toBeInTheDocument()
  })
})
