import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

export function PanelSkeleton({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} className="grid gap-4">
      <Skeleton className="h-24 rounded-xl" />
      <Skeleton className="h-40 rounded-xl" />
      <Skeleton className="h-40 rounded-xl" />
    </div>
  )
}

/** Shown when the first load fails; a later failed refresh keeps the data already on screen. */
export function PanelError({ what, error, retry }: { what: string; error: unknown; retry: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
      <span className="font-medium">{what} could not be loaded.</span>
      <span className="text-muted-foreground">{error instanceof ApiError ? error.detail : 'Something went wrong.'}</span>
      <Button size="sm" variant="outline" className="ml-auto" onClick={retry}>Retry</Button>
    </div>
  )
}
