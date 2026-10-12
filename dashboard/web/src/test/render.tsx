import { render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { AppProviders } from '@/AppProviders'
import { createQueryClient } from '@/lib/queryClient'
import { connectUnauthorized } from '@/lib/router'
import { routes } from '@/routes'

/** The whole app at `path`, with a fresh query cache and no retries (a failing request fails the test at once). */
export function renderApp(path = '/') {
  const client = createQueryClient()
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } })
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  connectUnauthorized(router)
  return { client, router, ...render(<AppProviders client={client}><RouterProvider router={router} /></AppProviders>) }
}

export function locationOf(router: ReturnType<typeof createMemoryRouter>): string {
  const { pathname, search } = router.state.location
  return pathname + search
}
