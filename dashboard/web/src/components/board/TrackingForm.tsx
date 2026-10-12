import { Loader2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { ApiError } from '@/api/client'
import { useSaveTracking, useTracking } from '@/api/hooks'
import type { Tracking } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  addDays, changesOf, type Draft, emailProblem, forgetDraft, isDirty, LIMITS, mailtoHref, recalledDraft, rememberDraft, toDraft,
} from '@/lib/tracking'

const FIELD = 'grid gap-1.5'
// a button that is not available keeps the keyboard focus (a disabled one drops it), so it is aria-disabled
const SOFT_DISABLED = 'aria-disabled:pointer-events-none aria-disabled:opacity-50'

/**
 * Notes, who you talk to, who referred you, and a date to follow up on, for one job. It is a draft on screen: nothing is
 * saved until Save. The draft survives closing the panel (see rememberDraft), and a save is refused, with the choice of
 * loading the latest or saving your changes on top, if the notes were changed elsewhere meanwhile.
 */
export default function TrackingForm({ id }: { id: number }) {
  const tracking = useTracking(id)
  if (tracking.isPending) return <p role="status" className="text-muted-foreground">Loading notes…</p>
  if (tracking.isError) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-2 text-muted-foreground">
        <span>The notes could not be loaded{tracking.error instanceof ApiError ? `: ${tracking.error.detail}` : ''}.</span>
        <Button size="sm" variant="outline" onClick={() => tracking.refetch()}>Retry</Button>
      </div>
    )
  }
  return <TrackingEditor id={id} initial={tracking.data} fetchLatest={async () => (await tracking.refetch()).data} />
}

function TrackingEditor({ id, initial, fetchLatest }: { id: number; initial: Tracking; fetchLatest: () => Promise<Tracking | undefined> }) {
  const save = useSaveTracking(id)
  // `base` is the stored version the draft is measured against; it moves on after a save or a "load the latest"
  const [base, setBase] = useState<Tracking>(initial)
  const [restored, setRestored] = useState(() => recalledDraft(id, initial.version) !== null)
  const [draft, setDraft] = useState<Draft>(() => recalledDraft(id, initial.version) ?? toDraft(initial))
  const [justSaved, setJustSaved] = useState(false)

  const dirty = isDirty(draft, base)
  const emailMsg = emailProblem(draft.contact_email)
  const conflict = save.error instanceof ApiError && save.error.status === 409
  const href = mailtoHref(base.contact_email)

  function edit(field: keyof Draft, value: string) {
    const next = { ...draft, [field]: value }
    setDraft(next)
    setJustSaved(false)
    if (isDirty(next, base)) rememberDraft(id, base.version, next)
    else forgetDraft(id)
  }

  function adopt(stored: Tracking) {
    setBase(stored)
    setDraft(toDraft(stored))
    setRestored(false)
    forgetDraft(id)
  }

  /** Save `changes` (fields only) against the stored `version`. */
  function send(version: string | null, changes: ReturnType<typeof changesOf>) {
    save.mutate({ version, changes }, {
      onSuccess: (stored) => { adopt(stored); setJustSaved(true) },
    })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!dirty || emailMsg || save.isPending) return
    send(base.version, changesOf(draft, base))
  }

  async function loadLatest() {
    const latest = await fetchLatest()
    if (latest) { adopt(latest); save.reset() }
  }

  /** Your changes on top of the latest: the fields you changed win, the ones you did not touch keep the latest value. */
  async function saveOnTop() {
    const latest = await fetchLatest()
    if (!latest) return
    const mine = changesOf(draft, base)         // what I changed, measured against what I started from
    setBase(latest)
    send(latest.version, mine)
  }

  return (
    <form onSubmit={submit} className="grid gap-3" aria-label="Notes and follow-up">
      {restored && dirty && <p role="status" className="text-xs text-muted-foreground">Your unsaved changes from earlier are back.</p>}
      <div className={FIELD}>
        <Label htmlFor={`notes-${id}`}>Notes</Label>
        <textarea
          id={`notes-${id}`}
          value={draft.notes}
          maxLength={LIMITS.notes}
          
          rows={4}
          onChange={(e) => edit('notes', e.target.value)}
          dir="auto"
          className="min-h-24 rounded-lg border border-input bg-background px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
        />
        <span className="text-right text-xs text-muted-foreground tabular-nums">{draft.notes.length}/{LIMITS.notes}</span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className={FIELD}>
          <Label htmlFor={`cname-${id}`}>Contact</Label>
          <Input id={`cname-${id}`} value={draft.contact_name} maxLength={LIMITS.contact_name} onChange={(e) => edit('contact_name', e.target.value)} dir="auto" autoComplete="off" />
        </div>
        <div className={FIELD}>
          <Label htmlFor={`cmail-${id}`}>Contact email</Label>
          <Input
            id={`cmail-${id}`}
            type="email"
            value={draft.contact_email}
            maxLength={LIMITS.contact_email}
            onChange={(e) => edit('contact_email', e.target.value)}
            aria-invalid={emailMsg !== null}
            aria-describedby={emailMsg ? `cmail-err-${id}` : undefined}
            autoComplete="off"
          />
          {emailMsg && <span id={`cmail-err-${id}`} aria-live="polite" className="text-xs text-destructive">{emailMsg}</span>}
        </div>
        <div className={FIELD}>
          <Label htmlFor={`ref-${id}`}>Referred by</Label>
          <Input id={`ref-${id}`} value={draft.referral} maxLength={LIMITS.referral} onChange={(e) => edit('referral', e.target.value)} dir="auto" autoComplete="off" />
        </div>
        <div className={FIELD}>
          <Label htmlFor={`due-${id}`}>Follow up on</Label>
          <Input id={`due-${id}`} type="date" value={draft.follow_up_on} onChange={(e) => edit('follow_up_on', e.target.value)} />
          <div className="flex flex-wrap gap-1.5">
            <Button type="button" size="xs" variant="outline" onClick={() => edit('follow_up_on', addDays(base.today, 3))}>In 3 days</Button>
            <Button type="button" size="xs" variant="outline" onClick={() => edit('follow_up_on', addDays(base.today, 7))}>In a week</Button>
            {draft.follow_up_on && <Button type="button" size="xs" variant="ghost" onClick={() => edit('follow_up_on', '')}>Clear date</Button>}
          </div>
        </div>
      </div>
      {href && base.contact_email && (
        <p className="text-sm"><a href={href} className="text-primary underline-offset-4 hover:underline">Write to {base.contact_email}</a></p>
      )}
      {save.isError && (
        <div role="alert" className="grid gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2 text-sm">
          <span>{save.error instanceof ApiError ? save.error.detail : 'The notes could not be saved.'}</span>
          {conflict && (
            <div className="flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => void loadLatest()}>Load the latest</Button>
              <Button type="button" size="sm" variant="outline" onClick={() => void saveOnTop()}>Save my changes on top</Button>
            </div>
          )}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" aria-disabled={!dirty || emailMsg !== null || save.isPending} className={SOFT_DISABLED}>
          {save.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}Save
        </Button>
        <Button
          type="button" size="sm" variant="ghost" aria-disabled={!dirty || save.isPending} className={SOFT_DISABLED}
          onClick={() => { if (dirty && !save.isPending) { setDraft(toDraft(base)); setRestored(false); forgetDraft(id); save.reset() } }}
        >
          Discard changes
        </Button>
        <span role="status" className="text-xs text-muted-foreground">{justSaved ? 'Saved.' : ''}</span>
      </div>
    </form>
  )
}
