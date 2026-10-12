import type { createBrowserRouter } from 'react-router-dom'
import { setUnauthorizedHandler } from '@/api/client'
import { loginPath } from '@/lib/safeNext'

type AppRouter = Pick<ReturnType<typeof createBrowserRouter>, 'navigate' | 'state'>

/** An expired or missing session anywhere sends the browser to the login page, which brings it back afterwards. */
export function connectUnauthorized(router: AppRouter): void {
  setUnauthorizedHandler(() => {
    const { pathname, search, hash } = router.state.location
    if (pathname === '/login') return
    void router.navigate(loginPath(pathname + search + hash), { replace: true })
  })
}
