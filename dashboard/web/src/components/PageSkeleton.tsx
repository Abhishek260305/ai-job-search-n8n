import { Skeleton } from '@/components/ui/skeleton'

export function PageSkeleton() {
  return (
    <main className="mx-auto max-w-6xl space-y-4 p-6" aria-busy="true" aria-label="Loading">
      <Skeleton className="h-8 w-48" />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-24" />)}
      </div>
      <Skeleton className="h-64" />
    </main>
  )
}
