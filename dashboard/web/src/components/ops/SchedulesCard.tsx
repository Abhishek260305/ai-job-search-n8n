import type { ScheduleRow } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DASH, formatDateTime, formatRelative } from '@/lib/format'
import { cadence, stageLabel } from '@/lib/ops'
import { TONE_CLASSES } from '@/lib/status'
import { useNow } from '@/lib/useNow'

function When({ value, never }: { value: string | null; never: string }) {
  if (!value) return <>{never}</>
  return <time dateTime={value} title={formatDateTime(value)}>{formatRelative(value)}</time>
}

/** A run that is due waits for the scheduler's next tick (every 5 minutes): "due", not "2m ago". */
function NextRun({ value }: { value: string | null }) {
  const now = useNow()
  if (value && new Date(value) <= now) return <span title={formatDateTime(value)}>due</span>
  return <When value={value} never={DASH} />
}

/** The four stages, one card each: how often, when it last ran and when it runs next. */
export default function SchedulesCard({ schedules, paused, timezone }: { schedules: ScheduleRow[]; paused: boolean; timezone: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Schedules</CardTitle>
        <p className="text-sm text-muted-foreground">Times are relative to now; daily times are in {timezone}.</p>
      </CardHeader>
      <CardContent>
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Schedules">
          {schedules.map((s) => (
            <li key={s.name} className="grid content-start gap-1.5 rounded-xl border p-3 text-sm">
              <div className="flex items-center gap-2">
                <h3 className="font-medium">{stageLabel(s.name)}</h3>
                {!s.enabled && <Badge variant="secondary">Off</Badge>}
                {s.enabled && s.feature_off && <Badge variant="secondary" title="The scheduler skips this stage while its feature is switched off">Feature off</Badge>}
                {s.overdue && <Badge className={TONE_CLASSES.warning}>Overdue</Badge>}
              </div>
              <p className="text-muted-foreground">{cadence(s)}{s.batch ? ` · ${s.batch} per run` : ''}{s.model ? ` · ${s.model}` : ''}</p>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
                <dt className="text-muted-foreground">Last run</dt>
                <dd><When value={s.last_run_at} never="never" /></dd>
                <dt className="text-muted-foreground">Next run</dt>
                <dd>
                  {!s.enabled || s.feature_off ? DASH : paused ? 'paused' : <NextRun value={s.next_run_at} />}
                </dd>
              </dl>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
