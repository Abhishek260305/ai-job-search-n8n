import { useStats } from '@/api/hooks'
import { formatDate } from '@/lib/format'

/** The last 12 weeks, newest first: jobs found, shortlisted and applied to. Collapsed by default. */
export default function WeeklyTable() {
  const stats = useStats()
  if (!stats.data) return null
  const weeks = [...stats.data.weekly].reverse()
  return (
    <details className="rounded-xl border bg-card p-3">
      <summary className="cursor-pointer text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50">Week by week</summary>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Jobs found, shortlisted and applied to, by week (weeks start on Monday, {stats.data.timezone})</caption>
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th scope="col" className="py-1 pr-4 font-normal">Week of</th>
              <th scope="col" className="px-2 py-1 text-right font-normal">Found</th>
              <th scope="col" className="px-2 py-1 text-right font-normal">Shortlisted</th>
              <th scope="col" className="py-1 pl-2 text-right font-normal">Applied</th>
            </tr>
          </thead>
          <tbody>
            {weeks.map((w) => (
              <tr key={w.week_start} className="border-t">
                <th scope="row" className="py-1.5 pr-4 text-left font-normal">{formatDate(w.week_start)}</th>
                <td className="px-2 py-1.5 text-right tabular-nums">{w.found}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{w.shortlisted}</td>
                <td className="py-1.5 pl-2 text-right tabular-nums">{w.applied}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}
