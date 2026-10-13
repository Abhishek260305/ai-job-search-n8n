import { useIsFetching, useQueryClient } from '@tanstack/react-query'
import { Download, LogOut, RefreshCw } from 'lucide-react'
import { NavLink, useNavigate } from 'react-router-dom'
import { keys, useBoard, useEmailCount, useLogout, usePipeline, useSpend, useSummary } from '@/api/hooks'
import ThemeToggle from '@/components/ThemeToggle'
import { Button } from '@/components/ui/button'
import { formatDateTime, formatRelative } from '@/lib/format'
import { exportHref, useJobsParams } from '@/lib/jobsParams'
import { cn } from '@/lib/utils'
import { useNow } from '@/lib/useNow'

function Updated({ at }: { at: string }) {
  const now = useNow()
  const when = new Date(at)
  // the server's clock may run a little ahead of the browser's; never say "in 1m"
  const shown = when > now ? now : when
  return (
    <span className="text-sm text-muted-foreground">
      Updated <time dateTime={at} title={formatDateTime(at)}>{formatRelative(shown, now)}</time>
    </span>
  )
}

export type Page = 'overview' | 'board' | 'email' | 'health' | 'spend' | 'settings' | 'search'

const PAGES: readonly { to: string; label: string; page: Page }[] = [
  { to: '/', label: 'Jobs', page: 'overview' },
  { to: '/board', label: 'Board', page: 'board' },
  { to: '/email', label: 'Email', page: 'email' },
  { to: '/health', label: 'Health', page: 'health' },
  { to: '/spend', label: 'Spend', page: 'spend' },
  { to: '/search', label: 'Search', page: 'search' },
  { to: '/settings', label: 'Settings', page: 'settings' },
]

/** How many email suggestions wait, next to the menu entry: only while the Gmail part is on and something waits. */
function EmailBadge() {
  const count = useEmailCount()
  if (!count.data?.enabled || count.data.pending < 1) return null
  return (
    <span className="ml-1.5 inline-flex min-w-5 justify-center rounded-full bg-primary px-1.5 text-xs font-semibold text-primary-foreground">
      <span aria-hidden="true">{count.data.pending}</span>
      <span className="sr-only"> ({count.data.pending} waiting)</span>
    </span>
  )
}

// One small component per page, so a page only asks for the data it shows (hooks cannot be conditional).
function OverviewUpdated() {
  const summary = useSummary()
  return summary.data ? <Updated at={summary.data.generated_at} /> : null
}
function HealthUpdated() {
  const health = usePipeline()
  return health.data ? <Updated at={health.data.generated_at} /> : null
}
function BoardUpdated() {
  const board = useBoard()
  return board.data ? <Updated at={board.data.generated_at} /> : null
}
function SpendUpdated() {
  const spend = useSpend()
  return spend.data ? <Updated at={spend.data.generated_at} /> : null
}

export default function AppHeader({ page = 'overview' }: { page?: Page }) {
  const client = useQueryClient()
  const { view } = useJobsParams()
  const logout = useLogout()
  const navigate = useNavigate()
  const queryKeys = page === 'overview' ? [keys.summary, keys.allJobs] : page === 'board' ? [keys.board, keys.stats] : page === 'email' ? [keys.emailAll, keys.emailCount] : [page === 'health' ? keys.pipeline : page === 'spend' ? keys.spend : page === 'search' ? keys.search : keys.settings]
  const fetching = useIsFetching({ predicate: (q) => queryKeys.some((k) => k[0] === q.queryKey[0]) }) > 0

  function refresh() {
    for (const queryKey of queryKeys) void client.invalidateQueries({ queryKey })
  }

  return (
    <header className="border-b bg-background">
      <div className="mx-auto flex max-w-[96rem] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 md:px-6">
        <h1 className="font-heading text-xl font-semibold tracking-tight">Job search</h1>
        <nav aria-label="Sections" className="flex items-center gap-1">
          {PAGES.map((p) => (
            <NavLink
              key={p.to}
              to={p.to}
              end
              className={({ isActive }) => cn(
                'rounded-md px-2.5 py-1.5 text-sm font-medium outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50',
                isActive ? 'bg-muted text-foreground' : 'text-muted-foreground',
              )}
            >
              {p.label}
              {p.page === 'email' && <EmailBadge />}
            </NavLink>
          ))}
        </nav>
        {page === 'overview' ? <OverviewUpdated /> : page === 'board' ? <BoardUpdated /> : page === 'health' ? <HealthUpdated /> : page === 'spend' ? <SpendUpdated /> : null}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" onClick={refresh} aria-label="Refresh" aria-busy={fetching}>
            <RefreshCw className={fetching ? 'animate-spin' : undefined} aria-hidden="true" />
            <span className="hidden sm:inline">Refresh</span>
          </Button>
          {page === 'overview' && (
            <Button variant="outline" asChild>
              <a href={exportHref(view)} download aria-label="Export CSV">
                <Download aria-hidden="true" />
                <span className="hidden sm:inline">Export CSV</span>
              </a>
            </Button>
          )}
          <ThemeToggle />
          <Button
            variant="ghost"
            aria-label="Log out"
            disabled={logout.isPending}
            onClick={() => {
              logout.mutate()
              // leave first: nothing on this page may refetch between the cache being cleared and the page going away
              navigate('/login', { replace: true })
            }}
          >
            <LogOut aria-hidden="true" />
            <span className="hidden sm:inline">Log out</span>
          </Button>
        </div>
      </div>
    </header>
  )
}
