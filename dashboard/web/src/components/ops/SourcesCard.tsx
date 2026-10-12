import type { Health } from '@/api/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatCount, formatDateTime, formatRelative } from '@/lib/format'
import { sourceLabel } from '@/lib/jobsTable'

/** Boards and feeds that failed their last fetch; a long streak is the one to look at (it also raises an alert). */
export default function SourcesCard({ sources }: { sources: Health['sources'] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Boards and feeds</CardTitle>
        <p className="text-sm text-muted-foreground">
          {sources.tracked === 0
            ? 'None fetched yet: company boards and remote feeds appear here after their first fetch.'
            : `${formatCount(sources.tracked)} tracked, ${formatCount(sources.failing)} failing.`}
        </p>
      </CardHeader>
      <CardContent className="text-sm">
        {sources.items.length === 0 ? (
          sources.tracked > 0 && <p className="text-muted-foreground">Every board and feed answered its last fetch.</p>
        ) : (
          <ul className="grid gap-2" aria-label="Failing boards and feeds">
            {sources.items.map((f) => (
              <li key={`${f.source}/${f.key}`} className="grid gap-0.5 rounded-lg border p-2.5">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{sourceLabel(f.source)}{f.key ? ` / ${f.key}` : ''}</span>
                  <span className="text-muted-foreground">failed {f.failures} {f.failures === 1 ? 'time' : 'times'} in a row</span>
                </div>
                {f.last_error && <p className="break-words text-muted-foreground">{f.last_error}</p>}
                <p className="text-xs text-muted-foreground">
                  {f.last_ok_at
                    ? <>Last worked <time dateTime={f.last_ok_at} title={formatDateTime(f.last_ok_at)}>{formatRelative(f.last_ok_at)}</time></>
                    : 'Never worked yet'}
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
