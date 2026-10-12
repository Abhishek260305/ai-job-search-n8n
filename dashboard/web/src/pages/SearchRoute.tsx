import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// Loaded on demand, like the other pages, so the login page stays small.
const Search = lazy(() => import('@/pages/Search'))

export default function SearchRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Search />
    </Suspense>
  )
}
