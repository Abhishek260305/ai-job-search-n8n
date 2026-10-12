import { AlertTriangle, CheckCircle2, OctagonAlert } from 'lucide-react'
import type { HealthAlert } from '@/api/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { TONE_CLASSES } from '@/lib/status'
import { cn } from '@/lib/utils'

/** What needs attention, worst first. The words "Needs action" and "Heads up" carry the level, not only the colour. */
export default function AlertsCard({ alerts }: { alerts: HealthAlert[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Needs attention</CardTitle>
      </CardHeader>
      <CardContent>
        {alerts.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <CheckCircle2 className="size-4 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
            All clear: nothing needs your attention.
          </p>
        ) : (
          <ul className="grid gap-2" aria-label="Alerts">
            {alerts.map((a, i) => {
              const bad = a.level === 'bad'
              const Icon = bad ? OctagonAlert : AlertTriangle
              return (
                <li key={`${a.code}:${i}`} className={cn('flex items-start gap-2 rounded-lg px-3 py-2 text-sm', TONE_CLASSES[bad ? 'danger' : 'warning'])}>
                  <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <span>
                    <strong className="font-semibold">{bad ? 'Needs action' : 'Heads up'}: </strong>
                    {a.message}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
