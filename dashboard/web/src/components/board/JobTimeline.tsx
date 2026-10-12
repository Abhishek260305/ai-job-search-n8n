import { ApiError } from '@/api/client'
import { useState } from 'react'
import { useJobEvents, useUndoEntry } from '@/api/hooks'
import type { JobEvent, PipelineStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/format'
import { describeEvent } from '@/lib/board'

/** "Undo this change", with a second step: it puts the job back as it was and removes the entry for good. */
function UndoEntry({ jobId, entry }: { jobId: number; entry: JobEvent }) {
  const undo = useUndoEntry()
  const [asking, setAsking] = useState(false)
  if (!asking) {
    return <Button type="button" size="xs" variant="ghost" className="mt-1" onClick={() => setAsking(true)}>Undo this change</Button>
  }
  return (
    <div role="group" aria-label="Confirm the undo" className="mt-1 flex flex-wrap items-center gap-2 text-xs">
      <span>Put the job back as it was, and remove this entry for good?</span>
      <Button
        type="button" size="xs" variant="outline" aria-disabled={undo.isPending} className="aria-disabled:pointer-events-none aria-disabled:opacity-50"
        onClick={() => { if (!undo.isPending) undo.mutate({ id: jobId, expected: entry.to_status as PipelineStatus, eventId: entry.id }, { onSettled: () => setAsking(false) }) }}
      >
        Yes, undo
      </Button>
      <Button type="button" size="xs" variant="ghost" disabled={undo.isPending} autoFocus onClick={() => setAsking(false)}>No</Button>
    </div>
  )
}

/** The status changes of one job, oldest first. A backfilled start is marked: the time is the best we know. */
export default function JobTimeline({ id }: { id: number }) {
  const events = useJobEvents(id)
  if (events.isPending) return <p className="text-muted-foreground" role="status">Loading the history…</p>
  if (events.isError) {
    return <p role="alert" className="text-muted-foreground">The history could not be loaded{events.error instanceof ApiError ? `: ${events.error.detail}` : ''}.</p>
  }
  if (!events.data.length) return <p className="text-muted-foreground">No status changes recorded yet.</p>
  return (
    <ol aria-label="Status history" className="space-y-2 border-l pl-4">
      {events.data.map((e) => (
        <li key={e.id} className="relative">
          <span aria-hidden="true" className="absolute top-1.5 -left-[1.3rem] size-2 rounded-full bg-primary" />
          <p className="font-medium">{describeEvent(e)}</p>
          <p className="text-xs text-muted-foreground">
            <time dateTime={e.at}>{formatDateTime(e.at)}</time>
            {e.source === 'backfill' && ' (approximate: recorded when history tracking began)'}
          </p>
          {e.note && <p className="mt-0.5 whitespace-pre-wrap" dir="auto">{e.note}</p>}
          {e.undoable && <UndoEntry jobId={id} entry={e} />}
        </li>
      ))}
    </ol>
  )
}
