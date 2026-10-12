import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// Loaded on demand, like the overview, so the login page stays small.
const Health = lazy(() => import('@/pages/Health'))

export default function HealthRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Health />
    </Suspense>
  )
}
