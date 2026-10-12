import { useState } from 'react'
import { Pie, PieChart } from 'recharts'
import type { Summary } from '@/api/types'
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { donutDescription, donutSlices, totalOf } from '@/lib/charts'
import { formatCount } from '@/lib/format'

const SIZE = 192

export default function StatusDonut({ byStatus }: { byStatus: Summary['by_status'] }) {
  const [withShortlist, setWithShortlist] = useState(false)
  const slices = donutSlices(byStatus, withShortlist)
  const total = totalOf(slices)
  const data = slices.map((s) => ({ ...s, fill: s.color }))
  const drawn = slices.filter((s) => s.count > 0).length

  return (
    <Card>
      <CardHeader>
        <CardTitle>Applications by status</CardTitle>
        <CardAction className="flex items-center gap-2">
          <Checkbox id="donut-not-applied" checked={withShortlist} onCheckedChange={(v) => setWithShortlist(v === true)} />
          <Label htmlFor="donut-not-applied" className="text-sm font-normal">Include not applied</Label>
        </CardAction>
      </CardHeader>
      <CardContent>
        {total === 0 ? (
          <p className="py-10 text-center text-sm text-muted-foreground">
            No applications yet. Mark a job as applied from the table below.
          </p>
        ) : (
          <div className="flex flex-col items-center gap-6 sm:flex-row sm:justify-center">
            <div role="img" aria-label={donutDescription(slices)} className="relative shrink-0">
              <PieChart width={SIZE} height={SIZE} accessibilityLayer={false}>
                <Pie
                  data={data}
                  dataKey="count"
                  nameKey="label"
                  innerRadius={62}
                  outerRadius={92}
                  startAngle={90}
                  endAngle={-270}
                  paddingAngle={drawn > 1 ? 1 : 0}
                  stroke="none"
                  isAnimationActive={false}
                />
              </PieChart>
              <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center" aria-hidden="true">
                <span className="font-heading text-2xl font-semibold tabular-nums">{formatCount(total)}</span>
                <span className="text-xs text-muted-foreground">{withShortlist ? 'in total' : 'submitted'}</span>
              </div>
            </div>
            <ul aria-label="Legend" className="grid min-w-44 gap-1.5 text-sm">
              {slices.map((s) => (
                <li key={s.key} className="flex items-center gap-2">
                  <span className="size-3 shrink-0 rounded-sm" style={{ backgroundColor: s.color }} aria-hidden="true" />
                  <span>{s.label}</span>
                  <span className="ml-auto pl-4 font-medium tabular-nums">{formatCount(s.count)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
