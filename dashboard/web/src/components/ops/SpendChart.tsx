import type { Spend } from '@/api/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { chartData, chartDescription, formatMoney } from '@/lib/ops'

const W = 720
const H = 180
const LEFT = 44
const BOTTOM = 22
const TOP = 8

/**
 * Spend per day for the last 30 days, stacked by purpose. Plain SVG: nothing to load, and every bar has a native
 * tooltip. The numbers are also in a table underneath, for screen readers and for phones, where there is no hover.
 */
export default function SpendChart({ spend }: { spend: Pick<Spend, 'daily' | 'generated_at' | 'timezone'> }) {
  const { points, series, total } = chartData(spend)
  const max = Math.max(...points.map((p) => p.total), 0.01)
  const plotW = W - LEFT - 4
  const plotH = H - BOTTOM - TOP
  const step = plotW / points.length
  const barW = Math.max(2, step * 0.7)
  const active = points.filter((p) => p.total > 0)

  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Spend per day, last 30 days</CardTitle>
        <p className="text-sm text-muted-foreground">{formatMoney(total)} in total, Claude and Apify together.</p>
      </CardHeader>
      <CardContent className="grid gap-3">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={chartDescription(points, series, total)} className="h-auto w-full">
          <text x={LEFT - 6} y={TOP + 4} textAnchor="end" className="fill-muted-foreground text-[10px]">{formatMoney(max)}</text>
          <text x={LEFT - 6} y={TOP + plotH} textAnchor="end" className="fill-muted-foreground text-[10px]">$0</text>
          <line x1={LEFT} x2={W - 4} y1={TOP + plotH} y2={TOP + plotH} className="stroke-border" />
          {points.map((p, i) => {
            let y = TOP + plotH
            const x = LEFT + i * step + (step - barW) / 2
            return (
              <g key={p.day}>
                <title>{`${p.label}: ${formatMoney(p.total)}`}</title>
                {series.map((s) => {
                  const h = (Number(p[s.key]) / max) * plotH
                  if (!(h > 0)) return null
                  y -= h
                  return <rect key={s.key} x={x} y={y} width={barW} height={h} fill={s.color} />
                })}
              </g>
            )
          })}
          <text x={LEFT} y={H - 6} className="fill-muted-foreground text-[10px]">{points[0].label}</text>
          <text x={W - 4} y={H - 6} textAnchor="end" className="fill-muted-foreground text-[10px]">{points[points.length - 1].label}</text>
        </svg>
        {series.length > 0 && (
          <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {series.map((s) => (
              <li key={s.key} className="flex items-center gap-2">
                <span className="size-3 rounded-sm" style={{ backgroundColor: s.color }} aria-hidden="true" />
                {s.label}
              </li>
            ))}
          </ul>
        )}
        {active.length > 0 && (
          <details className="text-sm">
            <summary className="cursor-pointer rounded text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
              Show the numbers
            </summary>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full min-w-72 text-left">
                <thead>
                  <tr className="text-muted-foreground">
                    <th scope="col" className="py-1 pr-3 font-medium">Day</th>
                    {series.map((s) => <th key={s.key} scope="col" className="py-1 pr-3 text-right font-medium">{s.label}</th>)}
                    <th scope="col" className="py-1 text-right font-medium">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {[...active].reverse().map((p) => (
                    <tr key={p.day} className="border-t">
                      <th scope="row" className="py-1 pr-3 font-normal">{p.label}</th>
                      {series.map((s) => <td key={s.key} className="py-1 pr-3 text-right tabular-nums">{formatMoney(Number(p[s.key]))}</td>)}
                      <td className="py-1 text-right font-medium tabular-nums">{formatMoney(p.total)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        )}
      </CardContent>
    </Card>
  )
}
