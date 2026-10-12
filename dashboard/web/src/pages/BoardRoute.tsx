import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// Loaded on demand, like the other pages, so the login page stays small.
const Board = lazy(() => import('@/pages/Board'))

export default function BoardRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Board />
    </Suspense>
  )
}
