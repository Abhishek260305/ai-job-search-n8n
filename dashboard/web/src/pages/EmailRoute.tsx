import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// Loaded on demand, like the other pages, so the login page stays small.
const Email = lazy(() => import('@/pages/Email'))

export default function EmailRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Email />
    </Suspense>
  )
}
