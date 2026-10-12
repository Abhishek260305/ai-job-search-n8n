// The jobs table's rules, apart from its components so they can be checked directly.
import type { JobsParams, PipelineStatus } from '@/api/types'
import { formatCount } from '@/lib/format'
import { DEFAULT_VIEW, type JobsView } from '@/lib/jobsParams'
import { STATUS_META, type Tone } from '@/lib/status'

type Tab = NonNullable<JobsParams['tab']>
export type SortKey = NonNullable<JobsParams['sort']>

/** Tabs in the order they are shown. "All" exists so the "Jobs found" card has a tab to point at. */
export const TAB_LABELS: readonly { tab: Tab; label: string }[] = [
  { tab: 'shortlisted', label: 'Shortlisted' },
  { tab: 'applied', label: 'Applied' },
  { tab: 'scored', label: 'All scored' },
  { tab: 'filtered', label: 'Filtered out' },
  { tab: 'scam', label: 'Scam-blocked' },
  { tab: 'all', label: 'All' },
]

const BY_ORDER = (Object.keys(STATUS_META) as PipelineStatus[]).sort((a, b) => STATUS_META[a].order - STATUS_META[b].order)

/**
 * The statuses worth filtering by on each tab (the API's tab conditions in queries.py). Filtered-out and
 * scam-blocked jobs all share one status, so those tabs have no status filter.
 */
/** The tabs control one panel (the filters and the table), outside Radix's Tabs: these ids tie them together. */
export const JOBS_PANEL_ID = 'jobs-panel'
export const jobsTabId = (tab: Tab) => `jobs-tab-${tab}`

export function statusOptions(tab: Tab): PipelineStatus[] {
  switch (tab) {
    case 'shortlisted':
      return ['shortlisted', 'tailored', 'drafted', 'approved']
    case 'applied':
      // applied_at is kept when a submitted job is moved back, so approved and skipped can sit here too
      return ['applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn', 'approved', 'skipped']
    case 'filtered':
    case 'scam':
      return []
    case 'scored':
      return BY_ORDER.filter((s) => s !== 'new' && s !== 'filtered_out' && s !== 'scam_blocked')
    default:
      return BY_ORDER
  }
}

/** Text sorts start A to Z; numbers and dates start with the highest or newest. */
const FIRST_ORDER: Record<SortKey, 'asc' | 'desc'> = {
  score: 'desc', posted: 'desc', first_seen: 'desc', company: 'asc', title: 'asc', status: 'asc',
}

/** The sort and order to start with when a column is picked from a list (the phone's sort bar) rather than clicked. */
export function firstSort(key: SortKey): Pick<JobsView, 'sort' | 'order'> {
  return { sort: key, order: FIRST_ORDER[key] }
}

/** A click on a column header: the same column flips its order, another column starts at its natural order. */
export function nextSort(view: JobsView, key: SortKey): Pick<JobsView, 'sort' | 'order'> {
  if (view.sort === key) return { sort: key, order: view.order === 'asc' ? 'desc' : 'asc' }
  return { sort: key, order: FIRST_ORDER[key] }
}

/** "1–50 of 263"; "0 of 0" for nothing; a page past the end says so instead of showing "251–50". */
export function pageRange(page: number, pageSize: number, total: number, shown: number): string {
  if (total === 0 || shown === 0) return `0 of ${formatCount(total)}`
  const first = (page - 1) * pageSize + 1
  return `${formatCount(first)}–${formatCount(first + shown - 1)} of ${formatCount(total)}`
}

export function pageCount(pageSize: number, total: number): number {
  return Math.max(1, Math.ceil(total / pageSize))
}

/** Anything the user narrowed the tab with (not the tab, sort or paging). */
export function hasFilters(view: JobsView): boolean {
  return Boolean(view.q) || view.status.length > 0 || view.source.length > 0 || view.min_score !== undefined
    || view.remote || view.posted_days !== undefined
}

export function clearFilters(view: JobsView): JobsView {
  return {
    ...view, q: undefined, status: [], source: [], min_score: undefined, remote: false, posted_days: undefined,
    page: DEFAULT_VIEW.page,
  }
}

const EMPTY: Record<Tab, string> = {
  shortlisted: 'No shortlisted jobs right now. New matches arrive after each collect.',
  applied: 'Nothing applied yet. Mark a job as applied from the Shortlisted tab.',
  scored: 'No scored jobs yet. Scoring runs after each collect.',
  filtered: 'Nothing was filtered out.',
  scam: 'No scam-blocked jobs.',
  all: 'No jobs yet. They arrive after the first collect.',
}

export function emptyMessage(view: JobsView): string {
  return hasFilters(view) ? 'No jobs match these filters.' : EMPTY[view.tab]
}

const SOURCE_LABELS: Record<string, string> = {
  indeed: 'Indeed', naukri: 'Naukri', linkedin: 'LinkedIn', linkedin_apify: 'LinkedIn (Apify)', glassdoor: 'Glassdoor',
  ziprecruiter: 'ZipRecruiter', google: 'Google Jobs', greenhouse: 'Greenhouse', lever: 'Lever', ashby: 'Ashby',
  workable: 'Workable', smartrecruiters: 'SmartRecruiters', remotive: 'Remotive', remoteok: 'Remote OK',
  weworkremotely: 'We Work Remotely', himalayas: 'Himalayas', arbeitnow: 'Arbeitnow',
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source
}

/** Green at or above the threshold, amber within 10 points of it, grey below; neutral when there is no threshold. */
export function scoreTone(score: number | null, threshold: number | null): Tone {
  if (score === null) return 'muted'
  if (threshold === null) return 'neutral'
  if (score >= threshold) return 'success'
  return score >= threshold - 10 ? 'warning' : 'muted'
}

/** The dimension scores worth showing in the score tooltip: numbers only, in the order the scorer wrote them. */
export function dimensionScores(scores: Record<string, unknown> | null | undefined): [string, number][] {
  if (!scores || typeof scores !== 'object') return []
  return Object.entries(scores)
    .filter((e): e is [string, number] => typeof e[1] === 'number' && Number.isFinite(e[1]))
    .map(([name, value]) => [name.replace(/_/g, ' '), value])
}
