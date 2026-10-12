import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// The overview (table, menus, charts) loads on demand, so the login page stays small.
const Overview = lazy(() => import('@/pages/Overview'))

export default function OverviewRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Overview />
    </Suspense>
  )
}
