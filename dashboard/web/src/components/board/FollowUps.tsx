import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useSetFollowUpDate, useSetFollowUpDays } from '@/api/hooks'
import type { Board } from '@/api/types'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { addDays, dueText, mailtoHref } from '@/lib/tracking'

function DaysSetting({ days }: { days: number }) {
  const save = useSetFollowUpDays()
  const [value, setValue] = useState(String(days))
  const n = Number(value)
  const valid = Number.isInteger(n) && n >= 1 && n <= 90
  return (
    <form
      onSubmit={(e) => { e.preventDefault(); if (valid && n !== days) save.mutate(n) }}
      className="flex flex-wrap items-center gap-2 text-sm"
      aria-label="Follow-up reminder setting"
    >
      <label htmlFor="follow-up-days" className="text-muted-foreground">Remind me when an application has had no reply for</label>
      <Input
        id="follow-up-days" type="number" min={1} max={90} inputMode="numeric" value={value}
        onChange={(e) => setValue(e.target.value)} aria-invalid={!valid} className="h-7 w-16"
      />
      <span className="text-muted-foreground">days</span>
      <Button type="submit" size="xs" variant="outline" disabled={!valid || n === days || save.isPending}>
        {save.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}Save
      </Button>
    </form>
  )
}

/** The jobs due for a follow-up today, with the two things you do about one: open it, or set when to be reminded next. */
export default function FollowUps({ board, onOpen }: { board: Board; onOpen: (id: number) => void }) {
  const set = useSetFollowUpDate()
  // every row being saved, so clicking a second row does not release the first while its request is in flight
  const [busy, setBusy] = useState<ReadonlySet<number>>(new Set())
  const track = (id: number, on: boolean) => setBusy((current) => {
    const next = new Set(current)
    if (on) next.add(id); else next.delete(id)
    return next
  })
  const today = board.today                     // the server's day (the configured time zone), not the browser's

  function remind(id: number, days: number) {
    if (busy.has(id)) return

    track(id, true)
    set.mutateAsync({ id, date: addDays(today, days) }).catch(() => undefined).finally(() => track(id, false))
  }

  return (
    <section aria-labelledby="follow-ups-heading" className="space-y-2 rounded-xl border bg-card p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id="follow-ups-heading" className="font-heading text-sm font-semibold">
          Follow up{board.follow_ups_total > 0 ? ` (${board.follow_ups_total})` : ''}
        </h3>
        <DaysSetting key={board.follow_up_days} days={board.follow_up_days} />
      </div>
      {board.follow_ups.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing is due. Set a follow-up date on a job to be reminded.</p>
      ) : (
        <ul aria-label="Due for a follow-up" className="divide-y">
          {board.follow_ups.map((f) => {
            const href = mailtoHref(f.contact_email)
            return (
              <li key={f.id} className="flex flex-wrap items-center gap-x-3 gap-y-1.5 py-2 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button" onClick={() => onOpen(f.id)} data-job-title={f.id}
                      className="rounded-sm text-left font-medium outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50" dir="auto"
                    >
                      {f.title}
                    </button>
                    <span className="text-muted-foreground" dir="auto">{[f.company, f.location].filter(Boolean).join(' · ')}</span>
                    <StatusBadge status={f.pipeline_status} />
                  </div>
                  <p className="text-muted-foreground">
                    {dueText(f, today)}
                    {f.contact_name && <> · <span dir="auto">{f.contact_name}</span></>}
                    {f.contact_email && (href ? <> · <a href={href} className="text-primary underline-offset-4 hover:underline">{f.contact_email}</a></> : <> · {f.contact_email}</>)}
                    {f.referral && <> · referred by <span dir="auto">{f.referral}</span></>}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {busy.has(f.id) && <Loader2 className="size-4 animate-spin text-muted-foreground" role="img" aria-label="Saving" />}
                  <Button size="xs" variant="outline" disabled={busy.has(f.id)} onClick={() => remind(f.id, board.follow_up_days)}>
                    Followed up{' '}<span className="sr-only" dir="auto">on {f.title}: remind me again in {board.follow_up_days} days</span>
                  </Button>
                  <Button size="xs" variant="ghost" disabled={busy.has(f.id)} onClick={() => remind(f.id, 3)}>
                    Snooze 3 days{' '}<span className="sr-only" dir="auto">for {f.title}</span>
                  </Button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {board.follow_ups_total > board.follow_ups.length && (
        <p className="text-xs text-muted-foreground">Showing {board.follow_ups.length} of {board.follow_ups_total}.</p>
      )}
    </section>
  )
}
