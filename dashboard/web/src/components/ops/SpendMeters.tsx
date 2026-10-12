import type { Spend } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { formatMoney, meter, type MeterTone } from '@/lib/ops'
import { TONE_CLASSES } from '@/lib/status'
import { cn } from '@/lib/utils'

const BAR: Record<MeterTone, string> = {
  ok: 'bg-emerald-600 dark:bg-emerald-500',
  warn: 'bg-amber-500',
  bad: 'bg-red-600 dark:bg-red-500',
}

type Props = { label: string; spent: number; cap: number; remaining: number; warnPct: number | null; stopped: boolean }

function Meter({ label, spent, cap, remaining, warnPct, stopped }: Props) {
  const m = meter(spent, cap, warnPct, stopped)
  return (
    <li className="grid content-start gap-1.5 rounded-xl bg-card p-4 ring-1 ring-foreground/10">
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">{label}</span>
        {m.tone === 'bad' && <Badge className={TONE_CLASSES.danger}>Cap reached</Badge>}
        {m.tone === 'warn' && <Badge className={TONE_CLASSES.warning}>Nearly there</Badge>}
      </div>
      <p className="font-heading text-2xl font-semibold tabular-nums">
        {formatMoney(spent)} <span className="text-sm font-normal text-muted-foreground">of {formatMoney(cap)}</span>
      </p>
      <div
        role="progressbar"
        aria-label={`${label}: ${formatMoney(spent)} of ${formatMoney(cap)}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={m.width}
        aria-valuetext={`${m.pct}% used`}
        className="h-2 overflow-hidden rounded-full bg-muted"
      >
        <div className={cn('h-full rounded-full', BAR[m.tone])} style={{ width: `${m.width}%` }} />
      </div>
      <p className="text-xs text-muted-foreground tabular-nums">{m.pct}% used · {formatMoney(remaining)} left</p>
    </li>
  )
}

/** Claude and Apify against their caps, this month and today. Raising a cap stays a CLI or .env action. */
export default function SpendMeters({ spend }: { spend: Pick<Spend, 'claude' | 'apify'> }) {
  const { claude, apify } = spend
  const claudeStopped = claude.blocked
  return (
    <section aria-label="Spend against the caps">
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Meter label="Claude, this month" spent={claude.month_spent} cap={claude.monthly_cap} remaining={claude.month_remaining}
          warnPct={claude.warn_pct} stopped={claudeStopped && claude.blocked_reason === 'monthly cap reached'} />
        <Meter label="Claude, today" spent={claude.day_spent} cap={claude.daily_cap} remaining={claude.day_remaining}
          warnPct={null} stopped={claudeStopped && claude.blocked_reason === 'daily cap reached'} />
        <Meter label="Apify, this month" spent={apify.month_spent} cap={apify.monthly_cap} remaining={apify.month_remaining}
          warnPct={null} stopped={apify.blocked && apify.blocked_reason === 'Apify monthly cap reached'} />
        <Meter label="Apify, today" spent={apify.day_spent} cap={apify.daily_cap} remaining={apify.day_remaining}
          warnPct={null} stopped={apify.blocked && apify.blocked_reason === 'Apify daily cap reached'} />
      </ul>
      {(claude.blocked || apify.blocked) && (
        <p role="status" className="mt-2 text-sm text-muted-foreground">
          {[claude.blocked && `Claude: ${claude.blocked_reason}.`, apify.blocked && `${apify.blocked_reason}.`].filter(Boolean).join(' ')}{' '}
          Spending resumes by itself at the next day or month, or when you raise the cap with <code>scripts/jobsearch budget set</code>.
        </p>
      )}
    </section>
  )
}
