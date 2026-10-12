import type { ReactNode } from 'react'
import type { JobsParams, Summary } from '@/api/types'
import { useSummary } from '@/api/hooks'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { formatCount } from '@/lib/format'
import { useJobsParams } from '@/lib/jobsParams'
import { JOBS_PANEL_ID, jobsTabId, TAB_LABELS } from '@/lib/jobsTable'

type Tab = NonNullable<JobsParams['tab']>

/** Only where the summary's number is exactly the tab's total (the Shortlisted card counts fewer statuses). */
function countFor(tab: Tab, summary: Summary | undefined): number | undefined {
  if (!summary) return undefined
  if (tab === 'applied') return summary.cards.applied
  if (tab === 'all') return summary.cards.found
  return undefined
}

export default function JobsTabs() {
  const { view, update } = useJobsParams()
  const summary = useSummary()

  return (
    <Tabs value={view.tab} onValueChange={(tab) => update({ tab: tab as Tab, status: [] })}>
      <div className="overflow-x-auto">
        <TabsList aria-label="Job lists">
          {TAB_LABELS.map(({ tab, label }) => {
            const count = countFor(tab, summary.data)
            return (
              <TabsTrigger
                key={tab}
                value={tab}
                id={jobsTabId(tab)}
                aria-controls={JOBS_PANEL_ID}
                className="px-2.5"
                aria-label={count === undefined ? undefined : `${label} (${formatCount(count)})`}
              >
                {label}
                {count !== undefined && <span className="text-xs text-muted-foreground tabular-nums">{formatCount(count)}</span>}
              </TabsTrigger>
            )
          })}
        </TabsList>
      </div>
    </Tabs>
  )
}

/** What the tabs switch: the filters and the table, named by the selected tab. */
export function JobsTabPanel({ children }: { children: ReactNode }) {
  const { view } = useJobsParams()
  return (
    <div id={JOBS_PANEL_ID} role="tabpanel" aria-labelledby={jobsTabId(view.tab)} className="space-y-3">
      {children}
    </div>
  )
}
