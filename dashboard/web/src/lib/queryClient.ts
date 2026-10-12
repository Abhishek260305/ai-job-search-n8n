import { QueryClient } from '@tanstack/react-query'

// Data changes when the scraper runs (a few times a day) or when a status is edited here, so 30 seconds of
// freshness is plenty; coming back to the tab refetches, so a change made from Telegram or the CLI shows up.
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: true },
      mutations: { retry: false },
    },
  })
}
