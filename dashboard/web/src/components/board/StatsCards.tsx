import { useStats } from '@/api/hooks'
import { PanelError } from '@/components/ops/PanelState'
import { Skeleton } from '@/components/ui/skeleton'
import { responseRate } from '@/lib/board'
import { formatCount } from '@/lib/format'

function Card({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border bg-card p-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums">{value}</dd>
      {hint && <dd className="text-xs text-muted-foreground">{hint}</dd>}
    </div>
  )
}

export default function StatsCards() {
  const stats = useStats()
  if (stats.isPending) return <Skeleton role="status" aria-label="Loading the numbers" className="h-24 rounded-xl" />
  if (stats.isError) return <PanelError what="The numbers" error={stats.error} retry={() => stats.refetch()} />
  const s = stats.data
  return (
    <dl aria-label="Application numbers" className="grid grid-cols-2 gap-3 md:grid-cols-5">
      <Card label="Applied" value={formatCount(s.applied)} hint="everything you submitted" />
      <Card label="Heard back" value={responseRate(s)} hint={`${formatCount(s.responded)} of ${formatCount(s.applied)} answered`} />
      <Card
        label="Days to a first reply"
        value={s.avg_days_to_reply === null ? '—' : String(s.avg_days_to_reply)}
        hint={s.replies_measured ? `average of ${s.replies_measured} measured` : 'measured from now on'}
      />
      <Card label="Reached an interview" value={formatCount(s.reached_interview)} />
      <Card label="Offers" value={formatCount(s.reached_offer)} />
    </dl>
  )
}
