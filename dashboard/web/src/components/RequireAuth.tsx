import { Outlet } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { useMe } from '@/api/hooks'
import { PageSkeleton } from '@/components/PageSkeleton'
import { Button } from '@/components/ui/button'

/** Wraps every page that needs a session. A 401 is handled by the API client, which goes to the login page. */
export default function RequireAuth() {
  const me = useMe()

  // data we already have stays on screen when a background refetch fails (a network blip, an API restart)
  if (me.data) return <Outlet />

  if (me.isError && !(me.error instanceof ApiError && me.error.status === 401)) {
    return (
      <main className="mx-auto max-w-6xl p-6" role="alert">
        <h1 className="font-heading text-xl font-semibold">The dashboard is not reachable</h1>
        <p className="mt-2 text-muted-foreground">{me.error instanceof ApiError ? me.error.detail : String(me.error)}</p>
        <Button className="mt-4" variant="outline" onClick={() => me.refetch()}>Try again</Button>
      </main>
    )
  }

  return <PageSkeleton />
}
