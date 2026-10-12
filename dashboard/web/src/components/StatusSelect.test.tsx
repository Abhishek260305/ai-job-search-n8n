import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { JobRow } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { jobPage, jobRow, server } from '@/test/server'

type Patch = { body: Record<string, unknown>; header: string | null }

/**
 * A job list that holds one job and changes it the way the API does: the PATCH answers with `reply`, and on success
 * the next list request shows the new status. Records every PATCH.
 */
function backend(row: Partial<JobRow>, reply: (body: Record<string, unknown>) => Response | null | Promise<Response | null> = () => null) {
  let current = jobRow(1, row)
  const patches: Patch[] = []
  server.use(
    http.get('/api/jobs', () => HttpResponse.json(jobPage([current]))),
    http.patch('/api/jobs/1/status', async ({ request }) => {
      const body = await request.json() as Record<string, unknown>
      patches.push({ body, header: request.headers.get('X-Requested-With') })
      const answer = await reply(body)
      if (answer) return answer
      current = { ...current, pipeline_status: body.status as JobRow['pipeline_status'] }
      return HttpResponse.json({ id: 1, status: body.status })
    }),
  )
  return {
    patches,
    set: (next: Partial<JobRow>) => { current = { ...current, ...next } },
  }
}

const trigger = (label: string) => screen.findByRole('button', { name: `Status: ${label}. Change status` })

async function choose(from: string, to: string) {
  const user = userEvent.setup()
  await user.click(await trigger(from))
  await user.click(await screen.findByRole('menuitemradio', { name: to }))
  return user
}

describe('StatusSelect', () => {
  it('shows the current status and offers the editable ones in pipeline order', async () => {
    backend({ pipeline_status: 'applied' })
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await trigger('Applied'))
    const items = await screen.findAllByRole('menuitemradio')
    expect(items.map((i) => i.textContent)).toEqual(
      ['Ready to apply', 'Applied', 'In progress', 'Interview', 'Offer', 'Rejected', 'Withdrawn', 'Skipped'])
    expect(screen.getByRole('menuitemradio', { name: 'Applied' })).toHaveAttribute('aria-checked', 'true')
  })

  it('saves a forward move with the status it showed and the write header, then shows the reloaded status', async () => {
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/')
    await choose('Applied', 'Interview')
    expect(await screen.findByText('Marked Interview')).toBeInTheDocument()
    expect(await trigger('Interview')).toBeEnabled()
    expect(api.patches).toEqual([{ body: { status: 'interview', expected_status: 'applied' }, header: 'jobsearch-dashboard' }])
  })

  it('choosing the status it already has sends nothing', async () => {
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/')
    await choose('Applied', 'Applied')
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    expect(api.patches).toEqual([])
  })

  it('is disabled with a spinner while saving, and changes nothing on screen until the server answers', async () => {
    let answer!: () => void
    const answered = new Promise<void>((resolve) => { answer = resolve })
    backend({ pipeline_status: 'applied' }, async () => { await answered; return null })
    renderApp('/')
    await choose('Applied', 'Offer')
    const busy = await trigger('Applied')
    await waitFor(() => expect(busy).toBeDisabled())
    expect(busy).toHaveAttribute('aria-busy', 'true')
    expect(busy.querySelector('.animate-spin')).not.toBeNull()
    answer()
    expect(await screen.findByText('Marked Offer')).toBeInTheDocument()
    expect(await trigger('Offer')).toBeEnabled()
  })

  it('stays busy until the reloaded list has arrived, so the old status never looks saved', async () => {
    let release!: () => void
    const released = new Promise<void>((resolve) => { release = resolve })
    let reloading = false
    backend({ pipeline_status: 'applied' })
    renderApp('/')
    await trigger('Applied')
    server.use(http.get('/api/jobs', async () => {
      reloading = true
      await released
      return HttpResponse.json(jobPage([jobRow(1, { pipeline_status: 'offer' })]))
    }))
    await choose('Applied', 'Offer')
    await waitFor(() => expect(reloading).toBe(true))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(await trigger('Applied')).toBeDisabled()
    expect(screen.queryByText('Marked Offer')).not.toBeInTheDocument()
    release()
    expect(await screen.findByText('Marked Offer')).toBeInTheDocument()
    expect(await trigger('Offer')).toBeEnabled()
  })

  it('asks before a move back, and Cancel sends nothing', async () => {
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/')
    const user = await choose('Applied', 'Ready to apply')
    const dialog = await screen.findByRole('dialog', { name: 'Move back to Ready to apply?' })
    expect(dialog).toHaveTextContent('This job was already applied. Move it back to Ready to apply? The applied date is kept.')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.patches).toEqual([])
    expect(await trigger('Applied')).toBeInTheDocument()
  })

  it('a confirmed move back is sent and the dialog closes', async () => {
    const api = backend({ pipeline_status: 'interview' })
    renderApp('/')
    const user = await choose('Interview', 'Skipped')
    await user.click(within(await screen.findByRole('dialog', { name: 'Move back to Skipped?' })).getByRole('button', { name: 'Move back' }))
    expect(await screen.findByText('Marked Skipped')).toBeInTheDocument()
    expect(await trigger('Skipped')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.patches.map((p) => p.body)).toEqual([{ status: 'skipped', expected_status: 'interview' }])
  })

  it('does not ask for a move between statuses that were never applied', async () => {
    const api = backend({ pipeline_status: 'shortlisted' })
    renderApp('/')
    await choose('Not applied', 'Ready to apply')
    expect(await screen.findByText('Marked Ready to apply')).toBeInTheDocument()
    expect(await trigger('Ready to apply')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(api.patches.map((p) => p.body)).toEqual([{ status: 'approved', expected_status: 'shortlisted' }])
  })

  it('on a conflict, says what the status is now and shows it', async () => {
    const api = backend({ pipeline_status: 'applied' }, () => {
      api.set({ pipeline_status: 'interview' })
      return HttpResponse.json({ detail: 'the status was changed elsewhere', current_status: 'interview' }, { status: 409 })
    })
    renderApp('/')
    await choose('Applied', 'Rejected')
    expect(await screen.findByText('Changed elsewhere, now Interview. Reloaded.')).toBeInTheDocument()
    expect(await trigger('Interview')).toBeEnabled()
  })

  it('on a refusal, shows the reason and keeps the status', async () => {
    backend({ pipeline_status: 'applied' }, () => HttpResponse.json({ detail: 'job 1 is scam-blocked' }, { status: 403 }))
    renderApp('/')
    await choose('Applied', 'Offer')
    expect(await screen.findByText('job 1 is scam-blocked')).toBeInTheDocument()
    expect(await trigger('Applied')).toBeEnabled()
    expect(screen.queryByText(/^Marked /)).not.toBeInTheDocument()
  })

  it('a scam-blocked job cannot be changed, and says why', async () => {
    backend({ pipeline_status: 'scam_blocked', job_status: 'scam_blocked' })
    renderApp('/')
    const control = await screen.findByRole('button', { name: 'Status: Scam-blocked. Status changes are disabled' })
    expect(control).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Change status$/ })).not.toBeInTheDocument()
    await userEvent.setup().hover(control.parentElement!)
    expect((await screen.findAllByText('Scam-blocked: status changes are disabled')).length).toBeGreaterThan(0)
  })

  it('saves a change with a trimmed note, counting the characters', async () => {
    const api = backend({ pipeline_status: 'applied' })
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await trigger('Applied'))
    await user.click(await screen.findByRole('menuitem', { name: 'Add a note…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Change the status' })
    const select = within(dialog).getByLabelText('New status')
    expect(select).toHaveValue('applied')
    await user.selectOptions(select, 'interview')
    const note = within(dialog).getByLabelText('Note (optional)')
    expect(note).toHaveAttribute('maxLength', '500')
    await user.type(note, '  phone screen ')
    expect(within(dialog).getByText('15/500')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Marked Interview')).toBeInTheDocument()
    expect(await trigger('Interview')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.patches.map((p) => p.body)).toEqual([{ status: 'interview', expected_status: 'applied', note: 'phone screen' }])
  })

  it('the note dialog warns when its status is a move back', async () => {
    backend({ pipeline_status: 'offer' })
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await trigger('Offer'))
    await user.click(await screen.findByRole('menuitem', { name: 'Add a note…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Change the status' })
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeInTheDocument()
    await user.selectOptions(within(dialog).getByLabelText('New status'), 'approved')
    expect(dialog).toHaveTextContent('Move it back to Ready to apply? The applied date is kept.')
    expect(within(dialog).getByRole('button', { name: 'Move back' })).toBeInTheDocument()
  })

  it('a job that is not editable yet starts the note dialog with no status chosen, and Save stays off', async () => {
    backend({ pipeline_status: 'drafted' })
    renderApp('/')
    const user = userEvent.setup()
    await user.click(await trigger('Drafted, waiting for review'))
    await user.click(await screen.findByRole('menuitem', { name: 'Add a note…' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('New status')).toHaveValue('')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.type(within(dialog).getByLabelText('Note (optional)'), 'call back on Monday')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.selectOptions(within(dialog).getByLabelText('New status'), 'approved')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('using the menu or the dialog never opens the job', async () => {
    backend({ pipeline_status: 'applied' })
    const { router } = renderApp('/')
    const user = userEvent.setup()
    await user.click(await trigger('Applied'))
    await user.click(await screen.findByRole('menuitem', { name: 'Add a note…' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByLabelText('Note (optional)'))
    await user.click(within(dialog).getByText('Now: Applied.'))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(locationOf(router)).toBe('/')
  })
})
