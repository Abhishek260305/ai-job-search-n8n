// The jobs view lives in the address bar: refresh, back and forward keep it, and a link can be shared.
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { JobsParams, PipelineStatus } from '@/api/types'
import { STATUS_META } from '@/lib/status'

type Tab = NonNullable<JobsParams['tab']>
type SortKey = NonNullable<JobsParams['sort']>
type Source = NonNullable<JobsParams['source']>[number]

/** Every value is set: what the API would assume for a missing parameter, so it is left out of the URL. */
export type JobsView = Required<Omit<JobsParams, 'q' | 'min_score' | 'posted_days'>> &
  Pick<JobsParams, 'q' | 'min_score' | 'posted_days'>

export const TABS: readonly Tab[] = ['shortlisted', 'applied', 'scored', 'filtered', 'scam', 'all']
export const SORT_KEYS: readonly SortKey[] = ['score', 'posted', 'first_seen', 'company', 'title', 'status']
export const SOURCES: readonly Source[] = [
  'indeed', 'naukri', 'linkedin', 'linkedin_apify', 'glassdoor', 'ziprecruiter', 'google',
  'greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters',
  'remotive', 'remoteok', 'weworkremotely', 'himalayas', 'arbeitnow',
]
export const PAGE_SIZES = [25, 50, 100] as const
const STATUSES = Object.keys(STATUS_META) as PipelineStatus[]

export const DEFAULT_VIEW: JobsView = {
  tab: 'shortlisted', status: [], source: [], remote: false, sort: 'score', order: 'desc', page: 1, page_size: 50,
}

function oneOf<T extends string | number>(allowed: readonly T[], value: string | null, fallback: T): T {
  return allowed.find((a) => String(a) === value) ?? fallback
}

function int(value: string | null, min: number, max: number, digits = 5): number | undefined {
  if (value === null || !new RegExp(`^\\d{1,${digits}}$`).test(value)) return undefined
  const n = Number(value)
  return n >= min && n <= max ? n : undefined
}

function listOf<T extends string>(allowed: readonly T[], values: string[]): T[] {
  return allowed.filter((a) => values.includes(a))
}

/** The view a URL describes. A value the API would refuse (an old link, a typo) is dropped rather than sent. */
export function parseView(search: URLSearchParams): JobsView {
  const q = search.get('q')?.trim().slice(0, 100)
  return {
    tab: oneOf(TABS, search.get('tab'), DEFAULT_VIEW.tab),
    q: q || undefined,
    status: listOf(STATUSES, search.getAll('status')),
    source: listOf(SOURCES, search.getAll('source')),
    min_score: int(search.get('min_score'), 0, 100),
    remote: search.get('remote') === 'true',
    posted_days: int(search.get('posted_days'), 1, 365),
    sort: oneOf(SORT_KEYS, search.get('sort'), DEFAULT_VIEW.sort),
    order: search.get('order') === 'asc' ? 'asc' : 'desc',
    page: int(search.get('page'), 1, 10_000) ?? DEFAULT_VIEW.page,
    page_size: oneOf(PAGE_SIZES, search.get('page_size'), DEFAULT_VIEW.page_size),
  }
}

/** The query string for a view: defaults left out, lists as repeated keys, in a fixed order. */
export function viewSearch(view: JobsView, jobId: number | null = null): URLSearchParams {
  const search = new URLSearchParams()
  if (view.tab !== DEFAULT_VIEW.tab) search.set('tab', view.tab)
  if (view.q) search.set('q', view.q)
  view.status.forEach((s) => search.append('status', s))
  view.source.forEach((s) => search.append('source', s))
  if (view.min_score !== undefined) search.set('min_score', String(view.min_score))
  if (view.remote) search.set('remote', 'true')
  if (view.posted_days !== undefined) search.set('posted_days', String(view.posted_days))
  if (view.sort !== DEFAULT_VIEW.sort) search.set('sort', view.sort)
  if (view.order !== DEFAULT_VIEW.order) search.set('order', view.order)
  if (view.page !== DEFAULT_VIEW.page) search.set('page', String(view.page))
  if (view.page_size !== DEFAULT_VIEW.page_size) search.set('page_size', String(view.page_size))
  if (jobId !== null) search.set('job', String(jobId))
  return search
}

/** The CSV download for what the table shows, every page: the same filters and sort, no paging. */
export function exportHref(view: JobsView): string {
  const search = viewSearch({ ...view, page: DEFAULT_VIEW.page, page_size: DEFAULT_VIEW.page_size })
  const text = search.toString()
  return text ? `/api/export.csv?${text}` : '/api/export.csv'
}

/** The view in the URL, the open job (`?job=`), and ways to change them. Changing a filter starts again at page 1. */
export function useJobsParams() {
  const [search, setSearch] = useSearchParams()
  const view = useMemo(() => parseView(search), [search])
  const jobId = int(search.get('job'), 1, Number.MAX_SAFE_INTEGER, 15) ?? null

  const setView = useCallback(
    (next: JobsView, nextJob: number | null = null) => setSearch(viewSearch(next, nextJob)),
    [setSearch],
  )
  const update = useCallback(
    (patch: Partial<JobsView>) => setView({ ...view, page: DEFAULT_VIEW.page, ...patch }, jobId),
    [setView, view, jobId],
  )
  return { view, jobId, setView, update }
}
