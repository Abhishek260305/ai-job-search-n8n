import type { Spend } from '@/api/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatCount } from '@/lib/format'
import { formatMoney } from '@/lib/ops'

/** This month by model: what each model was called for and what it cost. */
export default function ModelsCard({ models }: { models: Spend['models'] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>This month by model</CardTitle>
      </CardHeader>
      <CardContent className="text-sm">
        {models.length === 0 ? (
          <p className="text-muted-foreground">Nothing has been spent this month.</p>
        ) : (
          <ul className="grid gap-2" aria-label="Models">
            {models.map((m) => (
              <li key={`${m.provider}/${m.model}`} className="flex flex-wrap items-baseline gap-x-4 gap-y-0.5 rounded-lg border p-2.5">
                <span className="min-w-0 flex-1 break-words font-medium">{m.provider === 'apify' ? 'Apify' : m.model}</span>
                <span className="text-muted-foreground tabular-nums">
                  {formatCount(m.calls)} {m.calls === 1 ? 'call' : 'calls'}
                  {m.input_tokens + m.output_tokens > 0 && <> · {formatCount(m.input_tokens)} in, {formatCount(m.output_tokens)} out</>}
                </span>
                <span className="font-medium tabular-nums">{formatMoney(m.cost_usd)}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
