import { lazy, Suspense } from 'react'
import { PageSkeleton } from '@/components/PageSkeleton'

// Loaded on demand, like the other pages, so the login page stays small.
const Settings = lazy(() => import('@/pages/Settings'))

export default function SettingsRoute() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <Settings />
    </Suspense>
  )
}
