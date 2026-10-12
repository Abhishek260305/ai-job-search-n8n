import type { Health } from '@/api/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DASH, formatCount, formatDateTime, formatRelative } from '@/lib/format'

/** The scoring queue and the jobs that ran out of attempts. Putting parked jobs back is a command for now. */
export default function QueueCard({ queue, parked }: Pick<Health, 'queue' | 'parked'>) {
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Scoring queue</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
          <dt className="text-muted-foreground">Waiting to be scored</dt>
          <dd className="tabular-nums">
            {formatCount(queue.waiting_to_score)}
            {queue.waiting_over_3h > 0 && <span className="text-muted-foreground"> ({formatCount(queue.waiting_over_3h)} for over 3 hours)</span>}
          </dd>
          <dt className="text-muted-foreground">Last scored</dt>
          <dd>
            {queue.last_scored_at
              ? <time dateTime={queue.last_scored_at} title={formatDateTime(queue.last_scored_at)}>{formatRelative(queue.last_scored_at)}</time>
              : DASH}
          </dd>
        </dl>
        <div>
          <h3 className="font-medium">Parked jobs{parked.total > 0 ? ` (${formatCount(parked.total)})` : ''}</h3>
          {parked.total === 0 ? (
            <p className="mt-1 text-muted-foreground">None: no job has run out of attempts.</p>
          ) : (
            <>
              <p className="mt-1 text-muted-foreground">
                These ran out of attempts and are skipped. Put them back with <code>scripts/jobsearch requeue score</code> or <code>requeue tailor</code>.
              </p>
              <ul className="mt-2 grid gap-1" aria-label="Parked jobs">
                {parked.items.map((p) => (
                  <li key={`${p.stage}-${p.job_id}`} className="flex flex-wrap gap-x-2">
                    <span className="rounded bg-muted px-1.5 text-xs leading-5">{p.stage}</span>
                    <span className="min-w-0 break-words">{p.title}{p.company ? ` @ ${p.company}` : ''}</span>
                    <span className="text-muted-foreground">attempts {p.claims}, bad replies {p.strikes}</span>
                  </li>
                ))}
              </ul>
              {parked.total > parked.items.length && (
                <p className="mt-1 text-muted-foreground">…and {formatCount(parked.total - parked.items.length)} more.</p>
              )}
            </>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
