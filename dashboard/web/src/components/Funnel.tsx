import { Bar, BarChart, LabelList, XAxis, YAxis } from 'recharts'
import type { Summary } from '@/api/types'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { funnelDescription, funnelRows, thresholdNote } from '@/lib/charts'

export default function Funnel({ funnel, threshold }: { funnel: Summary['funnel']; threshold: number | null }) {
  const rows = funnelRows(funnel)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Where jobs drop out</CardTitle>
        <CardDescription>Each step as a share of the one before.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <div role="img" aria-label={funnelDescription(rows)} className="h-64 w-full">
          <BarChart
            responsive
            layout="vertical"
            data={rows}
            style={{ width: '100%', height: '100%' }}
            margin={{ top: 0, right: 88, bottom: 0, left: 0 }}
            accessibilityLayer={false}
          >
            <XAxis type="number" hide domain={[0, 'dataMax']} />
            <YAxis
              type="category"
              dataKey="label"
              width={108}
              tickLine={false}
              axisLine={false}
              interval={0}
              tick={{ className: 'fill-muted-foreground' }}
            />
            <Bar dataKey="count" fill="#0072B2" radius={4} minPointSize={2} isAnimationActive={false}>
              <LabelList dataKey="text" position="right" className="fill-foreground text-xs" />
            </Bar>
          </BarChart>
        </div>
        <p className="text-xs text-muted-foreground">{thresholdNote(threshold)}</p>
      </CardContent>
    </Card>
  )
}
