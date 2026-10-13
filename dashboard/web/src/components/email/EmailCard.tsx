import { useId, useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import { useAcceptEmail, useDecideEmail, useUndoEntry } from '@/api/hooks'
import type { EmailItem } from '@/api/types'
import StatusBadge from '@/components/StatusBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { CONFIDENCE_LABEL, flagLabel, isCareFlag, proposalSentence } from '@/lib/email'
import { formatDateTime, formatRelative } from '@/lib/format'
import { statusLabel } from '@/lib/status'

/**
 * One suggestion. The subject and the excerpt are text from an email (a stranger's): they are shown as plain text, never as markup
 * and never as a link. Accepting is the same change as a click on the board (history, Undo); a suggestion with a trust flag or low
 * confidence asks for a second look first, and a stale one (the job moved on) can only be dismissed.
 */
export default function EmailCard({ item, onOpen }: { item: EmailItem; onOpen: (jobId: number) => void }) {
  const accept = useAcceptEmail()
  const dismiss = useDecideEmail('dismiss')
  const restore = useDecideEmail('restore')
  const undo = useUndoEntry()
  const [checked, setChecked] = useState(false)
  const checkId = useId()
  const busy = accept.isPending || dismiss.isPending || restore.isPending || undo.isPending
  const care = item.flags.filter(isCareFlag)
  const notes = item.flags.filter((f) => !isCareFlag(f))
  const canAccept = item.can_accept && (!item.needs_ack || checked)

  return (
    <Card className="gap-3 py-4">
      <CardContent className="grid gap-3">
        <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
          <div className="min-w-0">
            <h3 className="font-heading text-base font-semibold">
              {item.company ?? 'Unknown company'}
              <span className="font-normal text-muted-foreground"> · {item.title}</span>
            </h3>
            <p className="text-sm text-muted-foreground">
              From <span className="font-mono text-foreground">{item.from_domain}</span> ·{' '}
              <time dateTime={item.received_at} title={formatDateTime(item.received_at)}>{formatRelative(item.received_at)}</time>
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <StatusBadge status={item.current_status} />
            <Button type="button" variant="ghost" size="sm" onClick={() => onOpen(item.job_id)} aria-label={`Open job: ${item.title} at ${item.company ?? 'unknown company'}`}>
              Open job
            </Button>
          </div>
        </div>

        <div className="grid gap-1 rounded-lg bg-muted/50 px-3 py-2 text-sm">
          <p><span className="font-medium">Subject: </span><span className="break-words">{item.subject}</span></p>
          <p className="break-words text-muted-foreground">{item.excerpt}</p>
        </div>

        <p className="text-sm">{proposalSentence(item)}</p>

        <ul className="flex flex-wrap items-center gap-1.5" aria-label="About this suggestion">
          <li><Badge variant="outline">{CONFIDENCE_LABEL[item.confidence]}</Badge></li>
          <li><Badge variant="outline">{item.source === 'model' ? 'Read by Claude' : 'Read by rules'}</Badge></li>
          {care.map((f) => (
            <li key={f}>
              <Badge variant="outline" className="gap-1 border-amber-500/50 bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200">
                <ShieldAlert className="size-3" aria-hidden="true" />{flagLabel(f)}
              </Badge>
            </li>
          ))}
          {notes.filter((f) => f !== 'classified_by_model').map((f) => (
            <li key={f}><Badge variant="outline">{flagLabel(f)}</Badge></li>
          ))}
        </ul>

        {item.state === 'pending' && item.stale && (
          <p role="note" className="rounded-md bg-amber-100 px-2 py-1.5 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
            This job has moved on (it is {statusLabel(item.current_status)} now), so this change no longer fits. You can only dismiss it.
          </p>
        )}
        {item.state === 'pending' && !item.can_accept && !item.stale && (
          <p role="note" className="rounded-md bg-muted px-2 py-1.5 text-sm">This job cannot be changed from here (it is {statusLabel(item.current_status)}).</p>
        )}

        {item.state === 'pending' && item.needs_ack && item.can_accept && (
          <div className="flex items-start gap-2 text-sm">
            <Checkbox id={checkId} checked={checked} onCheckedChange={(v) => setChecked(v === true)} className="mt-0.5" />
            <Label htmlFor={checkId} className="font-normal">
              I checked who sent this, and it looks genuine.
            </Label>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          {item.state === 'pending' && (
            <>
              <Button
                type="button"
                disabled={busy || !canAccept}
                aria-label={`Mark ${statusLabel(item.proposed_status)}: accept the suggestion for ${item.company ?? 'this job'}`}
                onClick={() => accept.mutate({ id: item.id, jobId: item.job_id, expected: item.current_status, acknowledged: checked })}
              >
                {accept.isPending ? 'Accepting…' : `Mark ${statusLabel(item.proposed_status)}`}
              </Button>
              <Button type="button" variant="outline" disabled={busy} onClick={() => dismiss.mutate(item.id)} aria-label={`Dismiss the suggestion from ${item.from_domain}`}>
                Dismiss
              </Button>
            </>
          )}
          {item.state === 'accepted' && (
            <>
              <span className="text-sm text-muted-foreground">
                Accepted <time dateTime={item.decided_at ?? undefined} title={formatDateTime(item.decided_at)}>{formatRelative(item.decided_at)}</time>: marked {statusLabel(item.proposed_status)}.
                {item.current_status !== item.proposed_status && ` The job is ${statusLabel(item.current_status)} now (the change was undone, or the job moved on).`}
              </span>
              {item.can_undo && item.event_id != null && (
                <Button type="button" variant="outline" size="sm" disabled={busy}
                  onClick={() => undo.mutate({ id: item.job_id, expected: item.proposed_status, eventId: item.event_id ?? undefined })}>
                  Undo
                </Button>
              )}
              {item.can_restore && (
                <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => restore.mutate(item.id)}>Review again</Button>
              )}
            </>
          )}
          {item.state === 'dismissed' && (
            <>
              <span className="text-sm text-muted-foreground">
                Dismissed <time dateTime={item.decided_at ?? undefined} title={formatDateTime(item.decided_at)}>{formatRelative(item.decided_at)}</time>.
              </span>
              {item.can_restore && <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => restore.mutate(item.id)}>Bring back</Button>}
            </>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
