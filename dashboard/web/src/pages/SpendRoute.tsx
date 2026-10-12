import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// Loaded on demand, like the overview, so the login page stays small.
const Spend = lazy(() => import('@/pages/Spend'))

export default function SpendRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Spend />
    </Suspense>
  )
}
