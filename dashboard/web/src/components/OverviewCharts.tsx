import { useSummary } from '@/api/hooks'
import { ChartsSkeleton } from '@/components/ChartsSkeleton'
import Funnel from '@/components/Funnel'
import StatusDonut from '@/components/StatusDonut'

/** The donut and the funnel, side by side on a laptop. A failed summary is reported once, by the cards above. */
export default function OverviewCharts() {
  const summary = useSummary()

  if (summary.isPending) return <ChartsSkeleton />
  if (summary.isError) return null

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <StatusDonut byStatus={summary.data.by_status} />
      <Funnel funnel={summary.data.funnel} threshold={summary.data.threshold} />
    </div>
  )
}
