import {
  createColumnHelper, flexRender, getCoreRowModel, type RowData, useReactTable,
} from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ArrowUpDown, ExternalLink, FileText, Mail } from 'lucide-react'
import type { MouseEvent } from 'react'
import { ApiError } from '@/api/client'
import { useJobs, useSummary } from '@/api/hooks'
import type { JobRow } from '@/api/types'
import NativeSelect from '@/components/NativeSelect'
import StatusSelect from '@/components/StatusSelect'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { DASH, domainOf, formatDate, formatDateTime, formatRelative, formatSalary, safeUrl } from '@/lib/format'
import { DEFAULT_VIEW, PAGE_SIZES, useJobsParams } from '@/lib/jobsParams'
import {
  clearFilters, dimensionScores, emptyMessage, firstSort, hasFilters, nextSort, pageCount, pageRange, scoreTone, type SortKey,
  sourceLabel,
} from '@/lib/jobsTable'
import { TONE_CLASSES } from '@/lib/status'
import { NARROW, useMediaQuery } from '@/lib/useMediaQuery'
import { cn } from '@/lib/utils'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface TableMeta<TData extends RowData> {
    threshold: number | null
    openJob: (id: number) => void
  }
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    label: string
    sortKey?: SortKey
    className?: string
  }
}

const ICON_LINK = 'inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50 [&_svg]:size-4'

function ScoreCell({ row, threshold }: { row: JobRow; threshold: number | null }) {
  if (row.score === null) return <span className="text-muted-foreground">{DASH}</span>
  const badge = (
    <span className={cn('inline-flex h-6 min-w-9 items-center justify-center rounded-md px-1.5 text-xs font-semibold tabular-nums', TONE_CLASSES[scoreTone(row.score, threshold)])}>
      {row.score}
    </span>
  )
  const dims = dimensionScores(row.scores)
  if (!dims.length && !row.verdict) return badge
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" aria-label={`Score ${row.score}: details`} className="rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          {badge}
        </button>
      </TooltipTrigger>
      <TooltipContent className="block">
        {row.verdict && <p className="mb-1 font-medium">{row.verdict}</p>}
        {dims.length > 0 && (
          <dl className="grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
            {dims.map(([name, value]) => (
              <div key={name} className="contents">
                <dt className="capitalize">{name}</dt>
                <dd className="text-right tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

function LinksCell({ row, large = false }: { row: JobRow; large?: boolean }) {
  const apply = safeUrl(row.apply_url)
  const site = domainOf(apply)
  // fingertip-sized targets on a phone
  const icon = cn(ICON_LINK, large && 'size-10 [&_svg]:size-5')
  return (
    <div className="flex items-center gap-0.5">
      {apply && (
        <a href={apply} target="_blank" rel="noopener noreferrer" className={icon} aria-label={`Apply on ${site}`} title={`Apply on ${site}`}>
          <ExternalLink aria-hidden="true" />
        </a>
      )}
      {row.has_resume && (
        <a href={`/api/jobs/${row.id}/files/resume`} target="_blank" rel="noopener noreferrer" className={icon} aria-label="Resume (PDF)" title="Resume (PDF)">
          <FileText aria-hidden="true" />
        </a>
      )}
      {row.has_cover_letter && (
        <a href={`/api/jobs/${row.id}/files/cover_letter`} target="_blank" rel="noopener noreferrer" className={icon} aria-label="Cover letter (PDF)" title="Cover letter (PDF)">
          <Mail aria-hidden="true" />
        </a>
      )}
      {row.needs_attention && (
        <span role="img" aria-label="Needs attention" title="Needs attention" className="ml-1 size-2 rounded-full bg-amber-500" />
      )}
    </div>
  )
}

const col = createColumnHelper<JobRow>()

const COLUMNS = [
  col.accessor('company', {
    meta: { label: 'Company', sortKey: 'company', className: 'max-w-44' },
    cell: ({ row }) => <span className="block truncate" title={row.original.company ?? undefined}>{row.original.company ?? DASH}</span>,
  }),
  col.accessor('title', {
    meta: { label: 'Title', sortKey: 'title', className: 'max-w-80' },
    cell: ({ row, table }) => (
      <div className="flex min-w-0 items-center gap-1.5">
        <button
          type="button"
          className="truncate rounded-sm text-left font-medium outline-none hover:underline focus-visible:underline focus-visible:ring-3 focus-visible:ring-ring/50"
          title={row.original.title}
          data-job-title={row.original.id}
          onClick={() => table.options.meta?.openJob(row.original.id)}
        >
          {row.original.title}
        </button>
        {row.original.scam_risk === 'medium' && <Badge className={TONE_CLASSES.warning}>Medium risk</Badge>}
      </div>
    ),
  }),
  col.accessor('location', {
    meta: { label: 'Location', className: 'max-w-48' },
    cell: ({ row }) => (
      <div className="flex min-w-0 items-center gap-1.5">
        <span className="truncate" title={row.original.location ?? undefined}>{row.original.location ?? DASH}</span>
        {row.original.is_remote && <Badge variant="secondary">Remote</Badge>}
      </div>
    ),
  }),
  col.display({
    id: 'salary',
    meta: { label: 'Salary' },
    cell: ({ row }) => (
      <span className="whitespace-nowrap tabular-nums">
        {formatSalary(row.original.salary_min, row.original.salary_max, row.original.salary_currency)}
      </span>
    ),
  }),
  col.accessor('score', {
    meta: { label: 'Score', sortKey: 'score' },
    cell: ({ row, table }) => <ScoreCell row={row.original} threshold={table.options.meta?.threshold ?? null} />,
  }),
  col.accessor('pipeline_status', {
    id: 'status',
    meta: { label: 'Status', sortKey: 'status' },
    cell: ({ row }) => <StatusSelect row={row.original} />,
  }),
  col.accessor('sources', {
    meta: { label: 'Sources' },
    cell: ({ row }) => {
      const [shown, rest] = [row.original.sources.slice(0, 2), row.original.sources.slice(2)]
      return (
        <div className="flex flex-wrap gap-1">
          {shown.map((s) => <Badge key={s} variant="outline">{sourceLabel(s)}</Badge>)}
          {rest.length > 0 && <Badge variant="outline" title={rest.map(sourceLabel).join(', ')}>+{rest.length}</Badge>}
        </div>
      )
    },
  }),
  col.display({
    id: 'posted',
    meta: { label: 'Posted', sortKey: 'posted' },
    cell: ({ row }) => {
      const r = row.original
      const when = r.posted_at ?? r.first_seen
      const now = new Date()
      // a board's date without a time zone can land a few hours ahead; a posting is never in the future
      const shown = new Date(when) > now ? now : when
      return (
        <div className="flex items-center gap-1.5 whitespace-nowrap">
          <time dateTime={when} title={`${r.posted_at ? 'Posted' : 'First seen'} ${formatDateTime(when)}`}>{formatRelative(shown, now)}</time>
          {r.closing_soon && (
            <Badge className={TONE_CLASSES.danger} title={r.deadline ? `Deadline ${formatDate(r.deadline)}` : undefined}>Closing soon</Badge>
          )}
        </div>
      )
    },
  }),
  col.accessor('filter_reason', {
    id: 'reason',
    meta: { label: 'Reason', className: 'max-w-64' },
    cell: ({ row }) => <span className="block truncate" title={row.original.filter_reason ?? undefined}>{row.original.filter_reason ?? DASH}</span>,
  }),
  col.accessor('scam_risk', {
    id: 'risk',
    meta: { label: 'Risk' },
    cell: ({ row }) => <span className="capitalize">{row.original.scam_risk ?? DASH}</span>,
  }),
  col.display({
    id: 'links',
    meta: { label: 'Links' },
    cell: ({ row }) => <LinksCell row={row.original} />,
  }),
]

/**
 * Clicks on links, buttons and form controls inside a row do their own thing, not open the details. So do clicks in
 * a menu or dialog the row opened: React bubbles those through the portal to the row, though they are not inside it.
 */
function fromControl(event: MouseEvent): boolean {
  const target = event.target as HTMLElement
  if (!event.currentTarget.contains(target)) return true
  return Boolean(target.closest('a, button, input, select, textarea, label, [role^="menuitem"]'))
}

const SORT_LABELS: Record<SortKey, string> = {
  score: 'Score', posted: 'Posted', first_seen: 'First seen', company: 'Company', title: 'Title', status: 'Status',
}
// the sorts the table's headers offer
const CARD_SORTS: SortKey[] = ['score', 'posted', 'company', 'title', 'status']

/** On a phone there are no column headers, so the sort lives in a small bar above the cards. */
function SortBar() {
  const { view, update } = useJobsParams()
  const keys = CARD_SORTS.includes(view.sort) ? CARD_SORTS : [...CARD_SORTS, view.sort]
  const Icon = view.order === 'asc' ? ArrowUp : ArrowDown
  return (
    <div className="flex items-center gap-2 text-sm">
      <label htmlFor="card-sort" className="text-muted-foreground">Sort by</label>
      <NativeSelect id="card-sort" className="h-10 flex-1" value={view.sort} onChange={(e) => update(firstSort(e.target.value as SortKey))}>
        {keys.map((k) => <option key={k} value={k}>{SORT_LABELS[k]}</option>)}
      </NativeSelect>
      <Button
        variant="outline"
        className="h-10 gap-1.5 px-3"
        aria-label={`Order: ${view.order === 'asc' ? 'ascending' : 'descending'}. Reverse`}
        onClick={() => update({ order: view.order === 'asc' ? 'desc' : 'asc' })}
      >
        <Icon className="size-4" aria-hidden="true" />
        {view.order === 'asc' ? 'Low first' : 'High first'}
      </Button>
    </div>
  )
}

/** One job as a card: everything the table row shows, stacked so it fits a phone without sideways scrolling. */
function JobCard({ row, threshold, selected, onOpen }: { row: JobRow; threshold: number | null; selected: boolean; onOpen: () => void }) {
  const when = row.posted_at ?? row.first_seen
  const now = new Date()
  const shown = new Date(when) > now ? now : when
  return (
    <li
      data-state={selected ? 'selected' : undefined}
      className="cursor-pointer rounded-xl border bg-card p-3 text-sm data-[state=selected]:border-ring data-[state=selected]:bg-muted/50"
      onClick={(e) => { if (!fromControl(e)) onOpen() }}
    >
      <div className="flex items-start gap-3">
        <ScoreCell row={row} threshold={threshold} />
        <div className="min-w-0 flex-1">
          <button
            type="button"
            className="line-clamp-2 rounded-sm text-left text-base font-medium leading-snug outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            data-job-title={row.id}
            onClick={onOpen}
          >
            {row.title}
          </button>
          <p className="mt-0.5 truncate text-muted-foreground">{row.company ?? DASH}</p>
        </div>
        <time dateTime={when} className="shrink-0 whitespace-nowrap text-xs text-muted-foreground" title={`${row.posted_at ? 'Posted' : 'First seen'} ${formatDateTime(when)}`}>
          {formatRelative(shown, now)}
        </time>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-muted-foreground">
        <span className="min-w-0 truncate">{row.location ?? DASH}</span>
        {row.is_remote && <Badge variant="secondary">Remote</Badge>}
        {formatSalary(row.salary_min, row.salary_max, row.salary_currency) !== DASH && (
          <span className="whitespace-nowrap tabular-nums text-foreground">{formatSalary(row.salary_min, row.salary_max, row.salary_currency)}</span>
        )}
        {row.scam_risk === 'medium' && <Badge className={TONE_CLASSES.warning}>Medium risk</Badge>}
        {row.closing_soon && <Badge className={TONE_CLASSES.danger}>Closing soon</Badge>}
      </div>
      {row.filter_reason && <p className="mt-1 text-xs text-muted-foreground">{row.filter_reason}</p>}
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <StatusSelect row={row} />
        <LinksCell row={row} large />
      </div>
    </li>
  )
}

function Pager({ total, shown }: { total: number; shown: number }) {
  const { view, update } = useJobsParams()
  const pages = pageCount(view.page_size, total)
  return (
    <nav aria-label="Pages" className="flex flex-wrap items-center justify-end gap-3 text-sm">
      <span className="tabular-nums text-muted-foreground" aria-live="polite">{pageRange(view.page, view.page_size, total, shown)}</span>
      <NativeSelect aria-label="Rows per page" value={view.page_size} onChange={(e) => update({ page_size: Number(e.target.value) })}>
        {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} per page</option>)}
      </NativeSelect>
      <Button variant="outline" size="sm" disabled={view.page <= 1} onClick={() => update({ page: view.page - 1 })}>Previous</Button>
      <Button variant="outline" size="sm" disabled={view.page >= pages} onClick={() => update({ page: view.page + 1 })}>Next</Button>
    </nav>
  )
}

export default function JobsTable() {
  const { view, jobId, setView, update } = useJobsParams()
  const jobs = useJobs(view)
  const summary = useSummary()
  const visible = { reason: view.tab === 'filtered', risk: view.tab === 'scam' }
  const narrow = useMediaQuery(NARROW)

  // eslint-disable-next-line react-hooks/incompatible-library -- the build does not use React Compiler
  const table = useReactTable({
    data: jobs.data?.items ?? [],
    columns: COLUMNS,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => String(row.id),
    manualPagination: true,
    manualSorting: true,
    state: { columnVisibility: visible },
    meta: { threshold: summary.data?.threshold ?? null, openJob: (id) => setView(view, id) },
  })

  if (jobs.isPending) {
    return (
      <div role="status" aria-label="Loading jobs" className="space-y-2">
        {Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-10" />)}
      </div>
    )
  }

  if (jobs.isError) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
        <span className="font-medium">The jobs could not be loaded.</span>
        <span className="text-muted-foreground">{jobs.error instanceof ApiError ? jobs.error.detail : 'Something went wrong.'}</span>
        <Button size="sm" variant="outline" className="ml-auto" onClick={() => jobs.refetch()}>Retry</Button>
      </div>
    )
  }

  const { items, total } = jobs.data

  if (total === 0) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed p-10 text-center text-sm text-muted-foreground">
        <p>{emptyMessage(view)}</p>
        {hasFilters(view) && <Button variant="outline" size="sm" onClick={() => setView(clearFilters(view), jobId)}>Clear filters</Button>}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {narrow && (
        <div className={cn('space-y-3 transition-opacity', jobs.isPlaceholderData && 'opacity-60')} aria-busy={jobs.isFetching}>
          <SortBar />
          {items.length === 0 ? (
            <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
              This page is past the end of the list.{' '}
              <Button variant="link" className="h-auto p-0" onClick={() => update({ page: DEFAULT_VIEW.page })}>Go to the first page</Button>
            </p>
          ) : (
            <ul className="space-y-2" aria-label="Jobs">
              {items.map((job) => (
                <JobCard
                  key={job.id}
                  row={job}
                  threshold={summary.data?.threshold ?? null}
                  selected={job.id === jobId}
                  onOpen={() => setView(view, job.id)}
                />
              ))}
            </ul>
          )}
        </div>
      )}
      {!narrow && (
      <div className={cn('rounded-xl ring-1 ring-foreground/10 transition-opacity', jobs.isPlaceholderData && 'opacity-60')} aria-busy={jobs.isFetching}>
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((group) => (
              <TableRow key={group.id}>
                {group.headers.map((header) => {
                  const meta = header.column.columnDef.meta!
                  const sorted = meta.sortKey && view.sort === meta.sortKey ? view.order : null
                  const Icon = sorted === 'asc' ? ArrowUp : sorted === 'desc' ? ArrowDown : ArrowUpDown
                  return (
                    <TableHead key={header.id} aria-sort={meta.sortKey ? (sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : 'none') : undefined}>
                      {meta.sortKey ? (
                        <button
                          type="button"
                          className="-ml-1 inline-flex items-center gap-1 rounded px-1 outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
                          onClick={() => update(nextSort(view, meta.sortKey!))}
                        >
                          {meta.label}
                          <Icon className={cn('size-3.5', !sorted && 'opacity-40')} aria-hidden="true" />
                        </button>
                      ) : meta.label}
                    </TableHead>
                  )
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={table.getVisibleLeafColumns().length} className="p-8 text-center text-muted-foreground">
                  This page is past the end of the list.{' '}
                  <Button variant="link" className="h-auto p-0" onClick={() => update({ page: DEFAULT_VIEW.page })}>Go to the first page</Button>
                </TableCell>
              </TableRow>
            ) : table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                data-state={row.original.id === jobId ? 'selected' : undefined}
                className="cursor-pointer"
                onClick={(e) => { if (!fromControl(e)) setView(view, row.original.id) }}
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id} className={cell.column.columnDef.meta?.className}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      )}
      <Pager total={total} shown={items.length} />
    </div>
  )
}
