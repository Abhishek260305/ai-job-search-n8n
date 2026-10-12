import { lazy, type MouseEvent, Suspense } from 'react'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import { ChartsSkeleton } from '@/components/ChartsSkeleton'
import JobDrawer from '@/components/JobDrawer'
import JobsFilters from '@/components/JobsFilters'
import JobsTable from '@/components/JobsTable'
import JobsTabs, { JobsTabPanel } from '@/components/JobsTabs'
import SummaryCards from '@/components/SummaryCards'

// Recharts is most of the bundle: the login page should not wait for it.
const OverviewCharts = lazy(() => import('@/components/OverviewCharts'))

/** Moves the keyboard past the cards and charts; a plain #jobs link would put the hash in the router's URL. */
function skipToJobs(event: MouseEvent<HTMLAnchorElement>) {
  event.preventDefault()
  document.getElementById('jobs')?.focus()
}

export default function Overview() {
  return (
    <>
      <a
        href="#jobs"
        onClick={skipToJobs}
        className="sr-only rounded-md bg-background px-3 py-2 text-sm font-medium shadow focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:ring-3 focus:ring-ring/50 focus:outline-none"
      >
        Skip to jobs
      </a>
      <AppHeader />
      <main className="mx-auto max-w-[96rem] space-y-6 p-4 md:p-6">
        <SummaryCards />
        <Suspense fallback={<ChartsSkeleton />}>
          <OverviewCharts />
        </Suspense>
        <section id="jobs" tabIndex={-1} aria-labelledby="jobs-heading" className="space-y-3 outline-none">
          <h2 id="jobs-heading" className="sr-only">Jobs</h2>
          <JobsTabs />
          <JobsTabPanel>
            <JobsFilters />
            <JobsTable />
          </JobsTabPanel>
        </section>
      </main>
      <AppFooter />
      <JobDrawer />
    </>
  )
}
